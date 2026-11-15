"""Durable file reservation, fixed identity and read-only reconciliation."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from examples.official_document_workflow.application.output_receipts import DELETE_PENDING
from examples.official_document_workflow.application.output_receipts import require_same_output
from examples.official_document_workflow.domain.documents import OutputNotWritten
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.shared.runtime.io import run_sync

_job = ContextVar('document_output_job', default=None)
_part = ContextVar('document_output_part', default='')


def output_job_identity():
    """Current server-owned delivery task, never a browser-supplied recipient."""
    return _job.get()


@contextmanager
def output_job(owner, key):
    token = _job.set((owner, key))
    try:
        yield
    finally:
        _job.reset(token)


@contextmanager
def output_part(identity):
    token = _part.set(str(identity))
    try:
        yield
    finally:
        _part.reset(token)


@dataclass(frozen=True)
class FileOutput:
    id: str
    owner: str
    name: str
    sha256: str
    size: int
    source: str
    kind: str
    content_type: str
    stream: object
    metadata: dict
    job: str | None

    @staticmethod
    def identity(owner, source, kind, name, explicit=None):
        scope = _job.get()
        job = scope[1] if scope and scope[0] == owner else None
        key = str(uuid.UUID(explicit)) if explicit else str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                json.dumps([owner, job, source, kind, name, _part.get()], ensure_ascii=False),
            ),
        ) if job else str(uuid.uuid4())
        return key, job


class OutputDelivery:
    def __init__(self, receipts, writer, finder):
        self.receipts, self.writer, self.finder = receipts, writer, finder

    async def store(self, output):
        task = asyncio.create_task(self._store(output))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def recover(self, receipt, *, expected=None):
        if receipt is None:
            raise OutputUncertain('缺少輸出收條，未自動重送。')
        require_same_output(receipt, expected if expected is not None else receipt)
        if receipt.state in {'not_written', 'deleted'} | DELETE_PENDING:
            raise OutputUncertain('輸出尚未完成或已刪除，未自動重送。')
        try:
            item = await self.finder(receipt)
        except Exception as error:
            raise OutputUncertain(f'輸出 {receipt.id} 需要核對，未自動重送。') from error
        if item is None:
            try:
                await run_sync(self.receipts.mark, receipt, 'unknown')
            except Exception:
                pass
            raise OutputUncertain(f'輸出 {receipt.id} 結果不明，未自動重送。')
        try:
            await self._confirm(receipt)
        except OutputUncertain:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Confirmed file receipt update deferred')
        return item

    async def _store(self, output):
        try:
            receipt, started = await run_sync(
                self.receipts.begin, output.id, output.owner, output.name,
                output.sha256, output.source, kind=output.kind, job=output.job, size=output.size,
            )
        except Exception as error:
            # The reservation itself can commit before its response is lost.
            try:
                record = await run_sync(self.receipts.get, output.id, output.owner)
                return await self.recover(record, expected=output)
            except Exception:
                raise OutputUncertain(f'輸出 {output.id} 預約結果需核對，未開始新寫入。') from error
        require_same_output(receipt, output)
        if not started:
            return await self.recover(receipt, expected=output)
        try:
            item = await self.writer(output)
            if item.id != receipt.id:
                raise OutputUncertain('輸出識別不符，需核對。')
        except OutputNotWritten:
            try:
                confirmed = await run_sync(self.receipts.mark, receipt, 'not_written')
                if confirmed is None or confirmed.state != 'not_written':
                    raise OutputUncertain('收條已變更，無法確認未寫入；請核對。')
            except Exception as error:
                raise OutputUncertain('未寫入狀態無法確認，請核對收條。') from error
            raise
        except Exception:
            return await self.recover(receipt, expected=output)
        try:
            await self._confirm(receipt)
        except OutputUncertain:
            raise
        except Exception:
            logging.getLogger(__name__).warning('Confirmed file receipt update deferred')
        return item

    async def _confirm(self, receipt):
        current = await run_sync(self.receipts.mark, receipt, 'stored')
        if current is not None:
            require_same_output(current, receipt)
            if current.state in DELETE_PENDING | {'deleted', 'not_written'}:
                raise OutputUncertain('輸出已轉入其他狀態，需核對。')
