"""Deletion intent, partial outcomes and read-only repair of ambiguous deletes."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from examples.official_document_workflow.application.output_receipts import MemoryOutputReceipts
from examples.official_document_workflow.services.file_access import FileAccess
from examples.official_document_workflow.services.output_deletion import OutputDeletion
from examples.official_document_workflow.services.output_repair import OutputRepair


class DeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'output.pdf'
        data = b'owned output bytes'
        self.path.write_bytes(data)
        self.repo = MemoryOutputReceipts()
        record,_ = self.repo.begin('file','owner','output.pdf',hashlib.sha256(data).hexdigest(),'source',
                                   kind='document_pdf',job='job',size=len(data))
        self.repo.mark(record,'stored')
        self.rows = {'file':SimpleNamespace(id='file',user_id='owner',path=str(self.path),filename='output.pdf',
            meta={'name':'output.pdf','file_hash':record.sha256,'data':{
                'kind':'document_pdf','source_file_id':'source','generated_by':'official_document_processor_pipe'}})}
        async def get(key,owner):
            row = self.rows.get(key)
            return row if row and row.user_id == owner else None
        async def drop(key):
            self.assertEqual(self.repo.get(key,'owner').state,'deleting')
            self.rows.pop(key,None)
        self.host = SimpleNamespace(get_file_by_id_and_user_id=AsyncMock(side_effect=get),
                                    delete_file_by_id=AsyncMock(side_effect=drop))
        self.storage = SimpleNamespace(get_file=lambda value:value,delete_file=Mock(side_effect=lambda path:Path(path).unlink()))
        self.files = FileAccess(self.host,self.storage)
        self.deletion = OutputDeletion(self.repo,self.files)
        self.publish = AsyncMock()
        self.repair = OutputRepair(self.repo,self.files,self.publish)

    def due(self):
        for review in self.repo._reviews.values():
            review['at'] = 0

    async def test_confirmed_delete_removes_access_before_object_and_marks_receipt(self):
        def remove(path):
            self.assertNotIn('file',self.rows)
            Path(path).unlink()
        self.storage.delete_file.side_effect = remove
        result = await self.deletion.delete('file','owner')
        self.assertEqual(result.state,'deleted')
        self.assertFalse(self.path.exists())
        self.assertEqual(self.repo.get('file','owner').state,'deleted')

    async def test_download_row_timeout_leaves_object_and_repair_never_replays_delete(self):
        async def uncertain(key):
            self.rows.pop(key,None)
            raise TimeoutError('download-row response lost')
        self.host.delete_file_by_id.side_effect = uncertain
        self.assertEqual((await self.deletion.delete('file','owner')).state,'unknown')
        self.assertTrue(self.path.exists())
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get('file','owner').state,'delete_unknown')
        self.storage.delete_file.assert_not_called()
        self.host.delete_file_by_id.assert_awaited_once()
        self.publish.assert_awaited_once_with('owner','job')

    async def test_storage_commit_timeout_is_reconciled_only_by_reading_absence(self):
        def uncertain(path):
            Path(path).unlink()
            raise TimeoutError('storage delete response lost')
        self.storage.delete_file.side_effect = uncertain
        self.assertEqual((await self.deletion.delete('file','owner')).state,'unknown')
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get('file','owner').state,'deleted')
        self.storage.delete_file.assert_called_once()
        self.host.delete_file_by_id.assert_awaited_once()
        self.publish.assert_awaited_once_with('owner','job')

    async def test_final_receipt_failure_preserves_unknown_until_read_only_repair(self):
        with patch.object(self.repo,'finish_delete',side_effect=OSError('database unavailable')):
            self.assertEqual((await self.deletion.delete('file','owner')).state,'unknown')
        self.due()
        await self.repair.run_once()
        self.assertEqual(self.repo.get('file','owner').state,'deleted')
        self.storage.delete_file.assert_called_once()

    async def test_unowned_or_changed_file_is_never_deleted(self):
        self.assertEqual((await self.deletion.delete('file','other')).state,'unknown')
        self.path.write_bytes(b'changed')
        self.assertEqual((await self.deletion.delete('file','owner')).state,'unknown')
        self.host.delete_file_by_id.assert_not_awaited()
        self.storage.delete_file.assert_not_called()

    async def test_cancelled_delete_drains_storage_and_receipt_before_returning(self):
        entered,release = threading.Event(),threading.Event()
        def remove(path):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test release timed out')
            Path(path).unlink()
        self.storage.delete_file.side_effect = remove
        task = asyncio.create_task(self.deletion.delete('file','owner'))
        self.assertTrue(await asyncio.to_thread(entered.wait,2))
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.repo.get('file','owner').state,'deleted')

    async def test_delete_review_distinguishes_residual_unavailable_mismatch_and_absence(self):
        record = self.repo.request_delete(self.repo.get('file','owner'),str(self.path))
        result = await self.files.inspect_deletion(record)
        self.assertEqual(result.outcome,'residual')
        self.assertEqual(result.proof['download_record'],True)
        self.assertEqual(result.proof['storage_object'],True)
        self.rows.clear()
        with patch.object(self.storage,'get_file',side_effect=OSError('private endpoint')):
            result = await self.files.inspect_deletion(record)
        self.assertEqual(result.outcome,'unavailable')
        self.assertNotIn('private endpoint',str(result))
        self.path.write_bytes(b'changed')
        result = await self.files.inspect_deletion(record)
        self.assertEqual(result.outcome,'mismatch')
        self.path.unlink()
        result = await self.files.inspect_deletion(record)
        self.assertEqual(result.outcome,'deleted')
        self.assertEqual(result.proof,{'download_record':False,'storage_object':False})
        self.storage.delete_file.assert_not_called()

    async def test_delete_review_refuses_a_changed_download_path(self):
        record = self.repo.request_delete(self.repo.get('file','owner'),str(self.path))
        self.rows['file'].path += '.changed'
        result = await self.files.inspect_deletion(record)
        self.assertEqual(result.outcome,'mismatch')
        self.assertEqual(result.reason,'storage_path_changed')
        self.host.delete_file_by_id.assert_not_awaited()

    async def test_successful_file_verification_keeps_actual_proof_and_file_from_one_read(self):
        record = self.repo.get('file','owner')
        with patch.object(self.files,'_digest',wraps=self.files._digest) as digest:
            result = await self.files.inspect(record)
        self.assertEqual(result.outcome,'verified')
        self.assertIs(result.file,self.rows['file'])
        self.assertEqual(result.proof,{'sha256':record.sha256,'bytes':self.path.stat().st_size})
        digest.assert_called_once()
        self.host.get_file_by_id_and_user_id.assert_awaited_once()
