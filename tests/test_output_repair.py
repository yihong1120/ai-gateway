"""Leased PostgreSQL review, fairness and result-notification retry without file writes."""
from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import examples.official_document_workflow.infrastructure.storage.migrations as _migrations_imports
from examples.official_document_workflow.application.output_verification import DeletionVerification
from examples.official_document_workflow.application.output_verification import FileVerification
from examples.official_document_workflow.infrastructure.storage.output_receipts import (
    PostgresOutputReceipts,
)
from examples.official_document_workflow.services.output_delivery import OutputDelivery
from examples.official_document_workflow.services.output_repair import OutputRepair
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_output_delivery import spec


class RepairTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.backend = self._workflow.store
        self.repo = PostgresOutputReceipts(lambda: self.backend)
        self.files = SimpleNamespace(
            inspect=AsyncMock(return_value=FileVerification('not_found')), find=AsyncMock(return_value=None), inspect_deletion=AsyncMock(return_value=DeletionVerification('residual')),
            remove=Mock(side_effect=AssertionError('repair must never delete')),
        )
        self.publish = AsyncMock()
        self.repair = OutputRepair(self.repo, self.files, self.publish)

    def begin(self, key='file', job='job'):
        return self.repo.begin(key, 'owner', 'file.pdf', 'hash', 'source', kind='document_pdf', job=job)[0]

    def due(self):
        with self.backend.db() as db:
            db.execute('UPDATE clarification_outputs SET next_review=0')

    async def test_receipt_commit_failure_is_recovered_without_another_writer(self):
        output = replace(spec(), owner='owner')
        writer = AsyncMock(return_value=SimpleNamespace(id=output.id))
        delivery = OutputDelivery(self.repo, writer, self.files.find)
        with patch.object(self.repo, 'mark', side_effect=OSError('receipt database unavailable')):
            await delivery.store(output)
        self.assertEqual(self.repo.get(output.id, 'owner').state, 'writing')
        self.due()
        self.files.inspect.return_value = FileVerification('verified')
        await self.repair.run_once()
        current = self.repo.get(output.id, 'owner')
        self.assertEqual(current.state, 'stored')
        self.assertFalse(current.projection_pending)
        writer.assert_awaited_once()
        self.publish.assert_awaited_once_with('owner', 'job-id')
        self.files.remove.assert_not_called()

    async def test_missing_receipts_back_off_and_do_not_starve_later_successes(self):
        for i in range(35):
            self.begin(f'bad-{i:02d}', job=None)
        self.begin('good', job=None)
        self.due()
        self.files.inspect.side_effect = lambda record: FileVerification(
            'verified' if record.id == 'good' else 'not_found',
        )
        self.assertEqual(await self.repair.run_once(), 32)
        self.assertLessEqual(await self.repair.run_once(), 4)
        self.assertEqual(self.repo.get('good', 'owner').state, 'stored')
        with self.backend.db() as db:
            rows = db.execute(
                "SELECT review_failures,next_review,review_token FROM clarification_outputs WHERE state='unknown'",
            ).fetchall()
        self.assertEqual(len(rows), 35)
        self.assertTrue(
            all(
                row['review_failures'] == 1 and row['next_review']
                > 0 and row['review_token'] is None for row in rows
            ),
        )
        self.files.remove.assert_not_called()

    async def test_expired_lease_and_stale_revision_cannot_complete_a_newer_receipt(self):
        record = self.begin()
        self.due()
        old = self.repo.claim_reviews(1)[0]
        self.assertEqual(self.repo.claim_reviews(1), [])
        with self.backend.db() as db:
            db.execute('UPDATE clarification_outputs SET review_until=0')
        current = self.repo.claim_reviews(1)[0]
        self.assertNotEqual(old.token, current.token)
        self.assertIsNone(self.repo.resolve_review(old, 'stored'))
        self.assertFalse(self.repo.finish_review(old, published=True))
        stored = self.repo.mark(record, 'stored')
        self.assertEqual(stored.state, 'stored')
        self.assertIsNone(self.repo.resolve_review(current, 'unknown'))
        self.assertFalse(self.repo.finish_review(current, published=True))
        self.assertTrue(self.repo.get(record.id, 'owner').projection_pending)

    async def test_projection_failure_is_durable_and_retried_with_backoff(self):
        record = self.begin()
        self.repo.mark(record, 'stored')
        self.publish.side_effect = [OSError('queue unavailable'), None]
        await self.repair.run_once()
        self.assertTrue(self.repo.get(record.id, 'owner').projection_pending)
        self.assertEqual(await self.repair.run_once(), 0)
        self.due()
        await self.repair.run_once()
        self.assertFalse(self.repo.get(record.id, 'owner').projection_pending)
        self.assertEqual(self.publish.await_count, 2)

    async def test_foreign_application_cannot_acknowledge_an_unowned_projection(self):
        record = self.begin()
        self.repo.mark(record, 'stored')
        self.publish.side_effect = [False, True]
        await self.repair.run_once()
        self.assertTrue(self.repo.get(record.id, 'owner').projection_pending)
        self.due()
        await self.repair.run_once()
        self.assertFalse(self.repo.get(record.id, 'owner').projection_pending)

    async def test_partial_delete_is_not_resurrected_or_replayed_by_review(self):
        record = self.begin()
        record = self.repo.mark(record, 'stored')
        intent = self.repo.request_delete(record, '/private/file.pdf')
        self.assertIsNotNone(intent)
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get(record.id, 'owner').state, 'delete_unknown')
        self.files.find.assert_not_awaited()
        self.files.remove.assert_not_called()
        self.files.inspect_deletion.return_value = DeletionVerification('deleted')
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get(record.id, 'owner').state, 'deleted')
        self.assertEqual(self.repo.for_jobs('other', ['job']), [])

    async def test_repeated_mismatch_is_persisted_and_parked_until_admin_verification(self):
        record = self.begin(job=None)
        self.files.inspect.return_value = FileVerification(
            'mismatch', 'content_mismatch', {'sha256': 'actual', 'bytes': 4},
        )
        for _ in range(3):
            self.due()
            await self.repair.run_once()
        current = self.repo.get(record.id, record.owner)
        self.assertTrue(current.review_required)
        self.assertEqual(self.repo.review_status(current)['proof'], {'sha256': 'actual', 'bytes': 4})
        self.assertEqual(self.repo.review_status(current)['mismatch_count'], 3)
        self.due()
        self.assertEqual(await self.repair.run_once(), 0)
        self.assertEqual(self.files.inspect.await_count, 3)
        self.repo.record_review(
            current, actor='administrator', method='webui_sha256', outcome='verified',
            proof={'sha256': 'hash', 'bytes': 4},
        )
        self.assertFalse(self.repo.get(record.id, record.owner).review_required)
        self.assertEqual(self.repo.get(record.id, record.owner).state, 'stored')

    async def test_transient_errors_remain_automatic_and_keep_a_safe_reason(self):
        record = self.begin(job=None)
        self.files.inspect.return_value = FileVerification('unavailable', 'storage_read_failed')
        for _ in range(4):
            self.due()
            await self.repair.run_once()
        self.assertFalse(self.repo.get(record.id, record.owner).review_required)
        status = self.repo.review_status(record)
        self.assertEqual(status['review_reason'], 'storage_read_failed')
        self.assertEqual(status['mismatch_count'], 0)
        self.files.inspect.return_value = FileVerification('verified')
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get(record.id, record.owner).state, 'stored')

    async def test_lease_renewal_rejects_expired_or_replaced_claims(self):
        record = self.begin()
        self.due()
        claim = self.repo.claim_reviews(1)[0]
        self.assertTrue(self.repo.renew_review(claim))
        with self.backend.db() as db:
            db.execute('UPDATE clarification_outputs SET review_until=0')
        self.assertFalse(self.repo.renew_review(claim))
        newer = self.repo.claim_reviews(1)[0]
        self.assertFalse(self.repo.renew_review(claim))
        self.assertTrue(self.repo.renew_review(newer))
        self.repo.mark(record, 'stored')
        self.assertFalse(self.repo.renew_review(newer))

    async def test_v8_upgrade_does_not_republish_already_acknowledged_results(self):
        record = self.begin()
        self.repo.mark(record, 'stored')
        await self.repair.run_once()
        with self.backend.db() as db:
            db.execute('DELETE FROM schema_migrations WHERE version>=9')
            db.execute(
                'ALTER TABLE clarification_outputs DROP COLUMN review_required, DROP COLUMN review_outcome, '
                'DROP COLUMN review_reason, DROP COLUMN review_evidence, DROP COLUMN mismatch_count',
            )
        _migrations_imports.migrate(self._eip_test_dsn, self._eip_test_schema)
        self.assertFalse(self.repo.get(record.id, record.owner).projection_pending)

    async def test_delete_review_persists_the_real_failure_classification(self):
        record = self.begin()
        stored = self.repo.mark(record, 'stored')
        pending = self.repo.request_delete(stored, '/private/output.pdf')
        self.files.inspect_deletion.return_value = DeletionVerification('unavailable', 'storage_status_unavailable')
        self.due()
        await self.repair.run_once()
        status = self.repo.review_status(pending)
        self.assertEqual(status['review_outcome'], 'unavailable')
        self.assertEqual(status['review_reason'], 'storage_status_unavailable')
        self.assertEqual(self.repo.get(record.id, record.owner).state, 'delete_unknown')
        self.files.remove.assert_not_called()
