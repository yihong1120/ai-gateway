"""Ordinary exports preserve ambiguous writes and never execute a second writer."""
from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from examples.official_document_workflow.application.output_receipts import (
    MemoryOutputReceipts,
)
from examples.official_document_workflow.domain.documents import OutputNotWritten
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.services.calendar_delivery import (
    CalendarOutputDelivery,
)
from examples.official_document_workflow.services.output_delivery import FileOutput
from examples.official_document_workflow.services.output_delivery import output_job
from examples.official_document_workflow.services.output_delivery import output_part
from examples.official_document_workflow.services.output_delivery import OutputDelivery
from examples.shared.runtime.modules import ModuleReference
from tests.test_official_document_processor_pipe import PIPE_MODULE as M

_workflow_services = ModuleReference('examples.official_document_workflow.services')


def spec(key='fixed-id', owner='alice'):
    data = b'generated document'
    return FileOutput(
        key, owner, 'letter.pdf', hashlib.sha256(data).hexdigest(), len(data),
        'source-id', 'document_pdf', 'application/pdf', io.BytesIO(data), {}, 'job-id',
    )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_commit_response_loss_recovers_file_without_resending(self):
        receipts = MemoryOutputReceipts()
        item = SimpleNamespace(id='fixed-id')
        writer = AsyncMock(side_effect=TimeoutError('private upstream detail'))
        delivery = OutputDelivery(receipts, writer, AsyncMock(return_value=item))
        self.assertIs(await delivery.store(spec()), item)
        self.assertIs(await delivery.store(spec()), item)
        writer.assert_awaited_once()
        self.assertEqual(receipts.get('fixed-id', 'alice').state, 'stored')

    async def test_unknown_file_is_not_replayed_and_other_owner_cannot_claim_it(self):
        receipts = MemoryOutputReceipts()
        writer, finder = AsyncMock(side_effect=TimeoutError()), AsyncMock(return_value=None)
        delivery = OutputDelivery(receipts, writer, finder)
        for _ in range(2):
            with self.assertRaises(OutputUncertain):
                await delivery.store(spec())
        with self.assertRaises(OutputUncertain):
            await delivery.store(spec(owner='bob'))
        writer.assert_awaited_once()
        self.assertEqual(receipts.get('fixed-id', 'alice').state, 'unknown')

    async def test_only_confirmed_prewrite_failure_reopens_writer(self):
        receipts = MemoryOutputReceipts()
        writer = AsyncMock(side_effect=[OutputNotWritten('before network'), SimpleNamespace(id='fixed-id')])
        delivery = OutputDelivery(receipts, writer, AsyncMock())
        with self.assertRaises(OutputNotWritten):
            await delivery.store(spec())
        self.assertEqual(receipts.get('fixed-id', 'alice').state, 'not_written')
        await delivery.store(spec())
        self.assertEqual(writer.await_count, 2)
        self.assertEqual(receipts.get('fixed-id', 'alice').state, 'stored')

    async def test_stale_nonwrite_transition_does_not_claim_a_safe_retry(self):
        receipts = MemoryOutputReceipts()
        writer = AsyncMock(side_effect=OutputNotWritten('before network'))
        delivery = OutputDelivery(receipts, writer, AsyncMock())

        def stale(record, state):
            return replace(record, state='unknown', revision=record.revision+1)
        with patch.object(receipts, 'mark', side_effect=stale):
            with self.assertRaises(OutputUncertain):
                await delivery.store(spec())
        writer.assert_awaited_once()

    async def test_cancelled_request_waits_for_writer_and_receipt_commit(self):
        receipts = MemoryOutputReceipts()
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(output):
            entered.set()
            await release.wait()
            return SimpleNamespace(id=output.id)
        delivery = OutputDelivery(receipts, write, AsyncMock())
        task = asyncio.create_task(delivery.store(spec()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(receipts.get('fixed-id', 'alice').state, 'stored')

    async def test_changed_content_or_deleted_file_is_not_recreated(self):
        receipts = MemoryOutputReceipts()
        writer = AsyncMock(return_value=SimpleNamespace(id='fixed-id'))
        delivery = OutputDelivery(receipts, writer, AsyncMock(return_value=SimpleNamespace(id='fixed-id')))
        await delivery.store(spec())
        with self.assertRaises(OutputUncertain):
            await delivery.store(replace(spec(), sha256='changed'))
        receipts.finish_delete(receipts.request_delete(receipts.get('fixed-id', 'alice'), '/private/output'))
        with self.assertRaises(OutputUncertain):
            await delivery.store(spec())
        writer.assert_awaited_once()

    def test_job_identity_is_owner_scoped_and_survives_recreation(self):
        with output_job('alice', 'job-one'):
            one = FileOutput.identity('alice', 'source', 'document_pdf', 'file.pdf')
        with output_job('alice', 'job-one'):
            self.assertEqual(FileOutput.identity('alice', 'source', 'document_pdf', 'file.pdf'), one)
            self.assertIsNone(FileOutput.identity('bob', 'source', 'document_pdf', 'file.pdf')[1])
        with output_job('alice', 'job-two'):
            self.assertNotEqual(FileOutput.identity('alice', 'source', 'document_pdf', 'file.pdf'), one)

    def test_same_named_attachments_use_stable_distinct_source_parts(self):
        with output_job('alice', 'job'):
            with output_part('attachment-one'):
                first = FileOutput.identity('alice', '', 'eip_source', 'letter.pdf')
            with output_part('attachment-two'):
                second = FileOutput.identity('alice', '', 'eip_source', 'letter.pdf')
            with output_part('attachment-one'):
                self.assertEqual(FileOutput.identity('alice', '', 'eip_source', 'letter.pdf'), first)
        self.assertNotEqual(first, second)

    async def test_reservation_failure_cannot_return_a_different_existing_output(self):
        receipts = MemoryOutputReceipts()
        writer, finder = AsyncMock(), AsyncMock(return_value=SimpleNamespace(id='fixed-id'))
        original = spec()
        record, _ = receipts.begin(
            original.id, original.owner, original.name, original.sha256, original.source,
            kind=original.kind, job=original.job, size=original.size,
        )
        receipts.mark(record, 'stored')
        delivery = OutputDelivery(receipts, writer, finder)
        for field, value in [
            ('sha256', 'changed'), ('source', 'changed'), ('kind', 'changed'), ('name', 'changed'),
            ('size', 999), ('job', 'another-job'),
        ]:
            with self.subTest(field=field), patch.object(receipts, 'begin', side_effect=OSError('reservation unavailable')):
                with self.assertRaises(OutputUncertain):
                    await delivery.store(replace(original, **{field: value}))
        finder.assert_not_awaited()
        writer.assert_not_awaited()

    async def test_pending_reminder_recovery_verifies_actual_bytes(self):
        exports = _workflow_services.attribute('exports')
        pipe = M.Pipe()
        continuation = SimpleNamespace(
            scope=SimpleNamespace(user_id='owner', chat_id='chat'), created_at=1,
            attachment_fingerprint='fingerprint', payload=SimpleNamespace(
                source_file_ids=('source',), csv_base_name='calendar.csv',
            ),
        )
        calendar = CalendarOutputDelivery(pipe._pending.outputs, pipe.exports, pipe.values)
        key = calendar.key(continuation)
        expected = b'original CSV'
        record, _ = pipe._pending.outputs.begin(
            key, 'owner', 'calendar_補充提醒.csv',
            hashlib.sha256(expected).hexdigest(), 'source',
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'calendar.csv'
            path.write_bytes(b'corrupted CSV')
            item = SimpleNamespace(
                id=key, user_id='owner', filename=record.name, path=str(path),
                meta={
                    'name': record.name, 'file_hash': record.sha256, 'data': {
                        'kind': record.kind, 'source_file_id': record.source, 'generated_by': 'official_document_processor_pipe',
                    },
                },
            )
            with patch.object(exports.Files, 'get_file_by_id_and_user_id', AsyncMock(return_value=item), create=True), \
                    patch.object(exports.Storage, 'get_file', side_effect=lambda value: value, create=True), \
                    patch.object(exports.Storage, 'upload_file', create=True) as upload:
                result = await calendar.recover(continuation)
            self.assertEqual(result.state, 'unknown')
            self.assertNotEqual(pipe._pending.outputs.get(key, 'owner').state, 'stored')
            upload.assert_not_called()

    async def test_ordinary_export_retains_object_after_file_database_commit_timeout(self):
        exports = _workflow_services.attribute('exports')
        pipe = M.Pipe()
        data = b'pdf bytes'
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'stored.pdf'
            saved = {}

            def upload(stream, name, tags):
                path.write_bytes(stream.read())
                return None, str(path)

            async def insert(owner, form):
                saved[form.id] = SimpleNamespace(
                    id=form.id, user_id=owner, filename=form.filename,
                    path=form.path, meta=form.meta,
                )
                raise TimeoutError('committed before response was lost')

            async def find(key, owner):
                value = saved.get(key)
                return value if value and value.user_id == owner else None
            with patch.object(exports.Storage, 'upload_file', side_effect=upload, create=True) as write, \
                    patch.object(exports.Storage, 'get_file', side_effect=lambda value: value, create=True), \
                    patch.object(exports.Storage, 'delete_file', create=True) as delete, \
                    patch.object(exports.Files, 'insert_new_file', side_effect=insert, create=True), \
                    patch.object(exports.Files, 'get_file_by_id_and_user_id', side_effect=find, create=True):
                with output_job('alice', 'export-job'):
                    first = await pipe.store_output(
                        user_id='alice', payload=data, display_name='letter.pdf',
                        content_type='application/pdf', source_file_id='source', kind='document_pdf',
                    )
                    second = await pipe.store_output(
                        user_id='alice', payload=data, display_name='letter.pdf',
                        content_type='application/pdf', source_file_id='source', kind='document_pdf',
                    )
                self.assertEqual(first.id, second.id)
                self.assertEqual(path.read_bytes(), data)
                write.assert_called_once()
                delete.assert_not_called()
                self.assertEqual(pipe._pending.outputs.get(first.id, 'alice').state, 'stored')
