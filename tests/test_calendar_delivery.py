"""Only confirmed non-writes may be retried; ambiguous writes are reconciled."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from examples.official_document_workflow.domain.documents import OutputNotWritten
from examples.official_document_workflow.domain.documents import OutputUncertain
from examples.official_document_workflow.domain.documents import ProcessingError
from examples.shared.runtime.modules import ModuleReference
from tests import test_official_document_processor_pipe as fixtures

_exports = ModuleReference('examples.official_document_workflow.services.exports')


class CalendarDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exports_module = _exports.load()
        fixture = fixtures.PendingCalendarClarificationPipeTests()
        fixture.setUp()
        self.pipe, self.scope = fixture.pipe, fixture.scope
        self.payload, self.source = fixture._pending_payload()
        self.store = self.pipe.clarifications.pending
        self.store.put(
            self.scope,
            attachment_fingerprint=self.pipe._attachment_fingerprint((self.source,)),
            payload=self.payload,
            ttl_seconds=60,
        )
        self.pipe._resolve_pending_reminder_choice = AsyncMock(
            return_value=fixtures.PIPE_MODULE.PendingReminderResolution(
                decision='all_verified_deadlines',
            ),
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.files = {}
        self.enterContext(
            patch.object(
                self.exports_module.Storage, 'get_file',
                side_effect=lambda path: path, create=True,
            ),
        )
        self.enterContext(
            patch.object(
                self.exports_module.Files,
                'get_file_by_id_and_user_id',
                AsyncMock(
                    side_effect=lambda key, owner: (
                        self.source if key == self.source.id else self.files.get(key)
                    ),
                ),
                create=True,
            ),
        )
        self.pipe.exports.store_output = AsyncMock(wraps=self.pipe.exports.store_output)
        self.set_writer(self.commit)

    def set_writer(self, effect):
        async def write(spec):
            if isinstance(effect, BaseException):
                raise effect
            spec.stream.seek(0)
            payload = spec.stream.read()
            result = effect(
                output_id=spec.id, display_name=spec.name, payload=payload, kind=spec.kind,
                source_file_id=spec.source, user_id=spec.owner,
            )
            return await result if inspect.isawaitable(result) else result
        self.pipe.exports.delivery.writer = AsyncMock(side_effect=write)

    def commit(self, **kwargs):
        path = self.root / (kwargs['output_id'] + '.csv')
        path.write_bytes(kwargs['payload'])
        item = types.SimpleNamespace(
            id=kwargs['output_id'],
            filename=kwargs['display_name'], user_id=kwargs['user_id'], path=str(path),
            meta={
                'file_hash': hashlib.sha256(kwargs['payload']).hexdigest(),
                'data': {
                    'generated_by': 'official_document_processor_pipe',
                    'kind': kwargs['kind'],
                    'source_file_id': kwargs['source_file_id'],
                },
            },
        )
        self.files[item.id] = item
        return item

    def upload(self, stream, name):
        path = self.root / name
        path.write_bytes(stream.read())
        return None, str(path)

    async def reply(self):
        return await self.pipe.clarifications.complete(
            body={'messages': [{'role': 'user', 'content': '全部'}]},
            user_id='user',
            scope=self.scope,
            request=None,
        )

    async def test_confirmed_nonwrite_restores_original_expiry_and_retry_identity(self):
        expiry = self.store.get(self.scope).expires_at
        self.set_writer(OutputNotWritten('confirmed before write'))
        self.assertIn('確認未建立', await self.reply())
        first_id = self.pipe.exports.store_output.await_args.kwargs['output_id']
        self.assertEqual(self.store.get(self.scope).expires_at, expiry)
        self.set_writer(self.commit)
        self.assertIn('更新後行事曆', await self.reply())
        self.assertEqual(
            self.pipe.exports.store_output.await_args.kwargs['output_id'], first_id,
        )
        self.assertEqual(self.store.outputs.get(first_id, 'user').state, 'stored')
        self.assertIsNone(self.store.get(self.scope))

    async def test_unknown_keeps_claim_and_recovers_late_commit_without_rewriting(self):
        captured = {}

        def uncertain(**kwargs):
            captured.update(kwargs)
            raise ProcessingError('unclassified storage failure')

        self.set_writer(uncertain)
        self.assertIn('未重送寫入', await self.reply())
        self.assertIsNone(self.store.get(self.scope))
        self.assertIsNotNone(self.store.review(self.scope))
        self.assertEqual(
            self.store.outputs.get(captured['output_id'], 'user').state, 'unknown',
        )
        self.assertIn('未重送寫入', await self.reply())
        self.commit(**captured)  # A delayed transaction becomes visible later.
        self.pipe._resolve_pending_reminder_choice.side_effect = AssertionError(
            'must not ask the model again',
        )
        output = await self.reply()
        self.assertIn(captured['output_id'], output)
        self.assertEqual(self.pipe.exports.store_output.await_count, 1)
        self.assertIsNone(self.store.review(self.scope))

    async def test_timeout_after_commit_is_reconciled_immediately(self):
        def committed_timeout(**kwargs):
            self.commit(**kwargs)
            raise OutputUncertain('timeout after commit')

        self.set_writer(committed_timeout)
        self.assertIn('更新後行事曆', await self.reply())
        self.assertEqual(len(self.files), 1)
        self.assertIsNone(self.store.review(self.scope))

    async def test_response_render_failure_keeps_receipt_for_reconnect(self):
        with patch.object(
            self.pipe.clarifications,
            '_render_pending_calendar_completion',
            side_effect=ProcessingError('render failed'),
        ):
            self.assertIn('未重送寫入', await self.reply())
        self.assertIsNotNone(self.store.review(self.scope))
        self.assertIn('已找回', await self.reply())
        self.assertEqual(self.pipe.exports.store_output.await_count, 1)

    async def test_cancelled_request_drains_write_and_leaves_result_for_reconnect(self):
        entered, finish = asyncio.Event(), asyncio.Event()

        async def write(**kwargs):
            entered.set()
            await finish.wait()
            return self.commit(**kwargs)

        self.set_writer(write)
        task = asyncio.create_task(self.reply())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.store.get(self.scope))
        self.assertIn('已找回', await self.reply())
        self.assertEqual(self.pipe.exports.store_output.await_count, 1)

    async def test_host_commit_timeout_never_deletes_a_committed_storage_object(self):
        self.pipe.exports.__dict__.pop('store_output')
        self.pipe.exports.delivery.writer = self.pipe.exports._write_file

        def insert(owner, form):
            item = types.SimpleNamespace(
                id=form.id, filename=form.filename, meta=form.meta, user_id=owner, path=form.path,
            )
            self.files[item.id] = item
            raise RuntimeError('lost response after database commit')

        with (
            patch.object(
                self.exports_module.Storage,
                'upload_file',
                Mock(side_effect=lambda stream, name, tags: self.upload(stream, name)),
                create=True,
            ),
            patch.object(
                self.exports_module.Storage, 'delete_file', Mock(), create=True,
            ) as delete,
            patch.object(
                self.exports_module.Files,
                'insert_new_file',
                AsyncMock(side_effect=insert),
                create=True,
            ),
        ):
            self.assertIn('更新後行事曆', await self.reply())
        delete.assert_not_called()
        self.assertEqual(len(self.files), 1)
