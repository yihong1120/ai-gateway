"""Completed file receipts survive a missed queue projection and stay owner-scoped."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from company_agent.api.queue import WorkQueue
from company_agent.api.service import Service
from company_agent.tests.test_agent import Fixture
from examples.official_document_workflow.application.output_catalog import OutputCatalog
from examples.official_document_workflow.infrastructure.storage.automation_store import store
from examples.official_document_workflow.infrastructure.storage.output_receipts import PostgresOutputReceipts
from examples.official_document_workflow.services.output_delivery import OutputDelivery
from tests.test_output_delivery import spec


class CatalogTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_receipt_catalog_recovers_output_after_queue_update_failure_and_restart(self):
        from dataclasses import replace
        queue = WorkQueue(self.private, self.base / 'uploads')
        self.addCleanup(queue.snapshots.close)
        key = queue.submit_document('alice','chat','turn',[{'id':'source'}],{},'revision')
        repo = PostgresOutputReceipts(store)
        writer = AsyncMock(return_value=SimpleNamespace(id='fixed-id'))
        await OutputDelivery(repo, writer, AsyncMock()).store(replace(spec(), job=key))
        with patch.object(queue, 'update', side_effect=OSError('projection unavailable')):
            with self.assertRaises(OSError):
                queue.update(key, document_result='{"files":[]}')
        current = Service(queue, None, None, None, document_outputs=OutputCatalog(repo).files_for_jobs)
        recovered = current.result('alice', key)
        self.assertEqual(recovered['files'][0]['id'], 'fixed-id')
        self.assertEqual(current.list('alice')[0]['files'], recovered['files'])
        self.assertEqual(current.list('bob'), [])
        with self.assertRaises(ValueError):
            current.result('bob', key)
        writer.assert_awaited_once()
        repo.finish_delete(repo.request_delete(repo.get('fixed-id','alice'),'/private/output'))
        self.assertEqual(current.result('alice', key)['files'], [])
