"""WebUI file boundary: owned metadata, content proof and explicit deletion I/O."""

from __future__ import annotations

from pathlib import Path

from examples.shared.runtime.io import run_sync
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.application.output_verification import (
    ContentProof,
    DeletionVerification,
    FileVerification,
    OutputMismatch,
    verify_metadata,
)


class FileAccess:
    def __init__(self, files, storage):
        self.files, self.storage = files, storage

    def _digest(self, path, record):
        local = self.storage.get_file(path)
        proof = ContentProof(record)
        with open(local, 'rb') as stream:
            while block := stream.read(64 * 1024):
                proof.update(block)
        return proof.finish()

    async def find(self, record):
        result = await self.inspect(record)
        if result.outcome == 'verified':
            return result.file
        if result.outcome == 'not_found':
            return None
        if result.outcome == 'mismatch':
            raise OutputMismatch(result.reason, result.proof)
        raise OutputUncertain('無法讀取輸出內容，請稍後核對。')

    def _metadata(self, record, item):
        verify_metadata(
            record,
            {
                'id': item.id,
                'user_id': getattr(item, 'user_id', None),
                'meta': item.meta,
                'filename': getattr(item, 'filename', ''),
            },
        )

    async def inspect(self, record):
        try:
            item = await self.files.get_file_by_id_and_user_id(record.id, record.owner)
            if item is None:
                return FileVerification('not_found', 'download_record_missing')
            self._metadata(record, item)
            proof = await run_sync(self._digest, item.path, record)
            return FileVerification('verified', proof=proof, file=item)
        except OutputMismatch as error:
            return FileVerification('mismatch', error.reason, error.proof)
        except Exception:
            return FileVerification('unavailable', 'storage_read_failed')

    async def object_present(self, record):
        if not record.path:
            return None

        def inspect():
            try:
                local = self.storage.get_file(record.path)
                return Path(local).exists()
            except FileNotFoundError:
                return False

        try:
            return await run_sync(inspect)
        except Exception:
            return None  # Failure to inspect storage is never proof of deletion.

    async def inspect_deletion(self, record):
        try:
            item = await self.files.get_file_by_id_and_user_id(record.id, record.owner)
            if item is not None:
                self._metadata(record, item)
                if item.path != record.path:
                    return DeletionVerification('mismatch', 'storage_path_changed')
            present = await self.object_present(record)
            if present is None:
                return DeletionVerification('unavailable', 'storage_status_unavailable')
            proof = {'download_record': item is not None, 'storage_object': present}
            if present:
                proof.update(await run_sync(self._digest, record.path, record))
            if item is None and not present:
                return DeletionVerification('deleted', proof=proof)
            return DeletionVerification('residual', 'deletion_incomplete', proof)
        except OutputMismatch as error:
            return DeletionVerification('mismatch', error.reason, error.proof)
        except Exception:
            return DeletionVerification('unavailable', 'storage_read_failed')

    async def deletion_complete(self, record):
        return (await self.inspect_deletion(record)).outcome == 'deleted'

    async def remove(self, record):
        # Remove access before the storage object; never leave a live download
        # row pointing at content deliberately removed by this service.
        item = await self.files.get_file_by_id_and_user_id(record.id, record.owner)
        if item is not None:
            if item.path != record.path:
                raise OutputUncertain('刪除目標路徑已變更，請核對。')
            await self.find(record)
            await self.files.delete_file_by_id(record.id)
        if (
            await self.files.get_file_by_id_and_user_id(record.id, record.owner)
            is not None
        ):
            raise OutputUncertain('下載紀錄刪除結果不明。')
        present = await self.object_present(record)
        if present is None:
            raise OutputUncertain('儲存物件狀態不明。')
        if present:
            await run_sync(self._digest, record.path, record)
            await run_sync(self.storage.delete_file, record.path)
