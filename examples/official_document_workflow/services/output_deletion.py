"""A persisted deletion intent precedes every destructive storage operation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from examples.official_document_workflow.application.output_receipts import DELETE_PENDING
from examples.shared.runtime.io import run_sync


@dataclass(frozen=True)
class DeletionResult:
    state: str
    output_id: str
    error_type: str | None = None


class OutputDeletion:
    def __init__(self, receipts, files):
        self.receipts, self.files = receipts, files

    async def delete(self, key, owner):
        task = asyncio.create_task(self._delete(key, owner))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _delete(self, key, owner):
        try:
            record = await run_sync(self.receipts.get, key, owner)
            if record is None:
                return DeletionResult('unknown', key, 'MissingReceipt')
            if record.state == 'deleted':
                return DeletionResult('deleted', key)
            if record.state in DELETE_PENDING:
                path = record.path
            else:
                item = await self.files.find(record)
                path = item.path if item else None
            if not path:
                return DeletionResult('unknown', key, 'MissingStoragePath')
            intent = await run_sync(self.receipts.request_delete, record, path)
            if intent is None:
                return DeletionResult('unknown', key, 'StaleReceipt')
            await self.files.remove(intent)
            completed = await run_sync(self.receipts.finish_delete, intent)
            if completed is None or completed.state != 'deleted':
                return DeletionResult('unknown', key, 'ReceiptNotConfirmed')
            return DeletionResult('deleted', key)
        except Exception as error:
            # Keep intent and original file list. Read-only repair can prove
            # both resources absent later; it never repeats a DELETE itself.
            return DeletionResult('unknown', key, type(error).__name__)
