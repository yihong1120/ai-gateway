"""Source preparation responsiveness and permission checks after parsing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from company_agent.api.service import Service
from company_agent.api.service import WorkQueue
from company_agent.config import Root
from company_agent.config import Settings
from company_agent.runtime.context import Context
from company_agent.services.files.access import FolderAccess
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.schemas import Read
from company_agent.services.files.schemas import Search
from company_agent.services.files.tools import file_tool
from company_agent.services.office.schemas import Edit
from company_agent.services.office.schemas import Text
from company_agent.tests.test_agent import Fixture


class SourceValidationTests(Fixture):
    def setUp(self):
        super().setUp()
        self.box = ToolBox(self.config, self.store, 'alice', self.job)

    def revoke(self, *args, **kwargs):
        self.config.roots[0].readers.clear()
        return [{'location': 'line:1', 'text': 'private source'}]

    def test_permission_revoked_during_text_parse_exposes_no_content_or_source(self):
        self.file('a.txt', b'private source')
        with patch('company_agent.services.files.operations.worker', side_effect=self.revoke):
            with self.assertRaisesRegex(ValueError, '沒有此資料位置的權限'):
                self.box.read_file(Read(root='project', path='a.txt'))
        self.assertEqual(self.box.sources, {})
        self.assertEqual(self.store.files('alice', self.job), [])

    def test_permission_revoked_during_content_search_exposes_no_matches(self):
        self.file('a.txt', b'private source')
        with patch('company_agent.services.files.operations.worker', side_effect=self.revoke):
            with self.assertRaisesRegex(ValueError, '沒有此資料位置的權限'):
                self.box.search_files(Search(root='project', query='private', mode='content'))
        self.assertEqual(self.box.sources, {})

    def test_permission_revoked_during_edit_does_not_register_output(self):
        self.file('a.docx', b'original')
        sha = hashlib.sha256(b'original').hexdigest()
        self.box.sources[('project', 'a.docx')] = {'root': 'project', 'path': 'a.docx', 'sha256': sha}
        args = Edit(
            root='project', path='a.docx', sha256=sha, name='changed.docx',
            location='paragraph:0/run:0', expected='old', replacement='new',
        )

        def transformed(operation, **kwargs):
            if operation == 'edit':
                self.config.roots[0].readers.clear()
                return b'changed'
            return []
        with patch('company_agent.services.files.operations.worker', side_effect=transformed):
            with self.assertRaisesRegex(ValueError, '沒有此資料位置的權限'):
                self.box.edit_text_run(args)
        self.assertEqual(self.store.files('alice', self.job), [])
        self.assertEqual((self.root / 'a.docx').read_bytes(), b'original')
        self.assertFalse((self.root / 'changed.docx').exists())


class SourcePreparationTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.queue = WorkQueue(self.private, self.base / 'sources')
        self.model = SimpleNamespace(subject='sub')
        self.service = Service(
            self.queue, AsyncMock(return_value={'id': 'alice'}),
            lambda _, model_name=None: self.model, self.settings,
        )

    def settings(self, row):
        return Settings(
            state_dir=str(self.private), roots=[
                Root(
                    name='uploads', path=str(self.queue.sources / row['id']),
                    readers=['alice'], model_access=True,
                ),
            ],
        )

    def followup(self, artifacts=1):
        previous = self.queue.submit('alice', 'sub', 'chat', 'prior', 'old', [('source.txt', b'SOURCE')])
        for index in range(artifacts):
            self.store.artifact('alice', self.job, f'result-{index}.txt', b'OUTPUT')
        self.queue.update(previous, status='done', engine_job=self.job)
        key = self.queue.submit('alice', 'sub', 'chat', 'next', 'read prior', [], [previous])
        return previous, key, self.queue.claim()

    def agent_patch(self):
        return patch('company_agent.api.service.DocumentAgent')

    async def wait_entered(self, event):
        async with asyncio.timeout(5):
            while not event.is_set():
                await asyncio.sleep(.01)

    async def test_many_artifacts_keep_manifest_and_reuse_one_folder_adapter(self):
        previous, key, row = self.followup(4)
        with self.agent_patch() as Agent, patch('company_agent.api.service.close_model', new=AsyncMock()), \
                patch('company_agent.api.service.FolderAccess', wraps=FolderAccess) as access:
            Agent.return_value.run = AsyncMock(return_value={'job_id': self.job, 'status': 'done'})
            await self.service.execute(row)
        self.assertEqual(access.call_count, 1)
        settings = Agent.call_args.args[0]
        root = settings.roots[-1]
        self.assertEqual(root.name, 'history_1')
        self.assertEqual(root.path, str(self.queue.sources / previous))
        self.assertEqual(root.writers, [])
        manifest = json.loads(Agent.return_value.run.await_args.args[1].split('\n')[-1])
        self.assertEqual(len(manifest['history_1']), 5)
        self.assertEqual(manifest['history_1'][0], {'path': 'input_1.txt', 'display_name': 'source.txt'})
        self.assertEqual({x['display_name'] for x in manifest['history_1'][1:]}, {f'result-{i}.txt' for i in range(4)})
        self.assertFalse(list((self.queue.sources / key).iterdir()))
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in (self.queue.sources / previous).iterdir()))
        self.assertEqual(self.queue.get('alice', key)['status'], 'done')

    async def test_slow_history_read_leaves_status_requests_responsive(self):
        _, key, row = self.followup()
        original = FolderAccess.read
        entered, release = threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Source read blocked the event loop')
            return original(*args, **kwargs)
        with self.agent_patch() as Agent, patch('company_agent.api.service.close_model', new=AsyncMock()), \
                patch.object(FolderAccess, 'read', side_effect=delayed, autospec=True):
            Agent.return_value.run = AsyncMock(return_value={'job_id': self.job, 'status': 'done'})
            task = asyncio.create_task(self.service.execute(row))
            try:
                await self.wait_entered(entered)
                public = await asyncio.wait_for(asyncio.to_thread(self.service.result, 'alice', key), 2)
                self.assertEqual(public['status'], 'running')
                Agent.assert_not_called()
                release.set()
                await asyncio.wait_for(task, 5)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_waits_for_cache_preparation_before_closing_model(self):
        previous, _, row = self.followup()
        original = self.queue.store.read_artifact
        entered, release = threading.Event(), threading.Event()

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Cancellation abandoned source preparation')
            return original(*args, **kwargs)
        with self.agent_patch() as Agent, patch('company_agent.api.service.close_model', new=AsyncMock()) as close, \
                patch.object(self.queue.store, 'read_artifact', side_effect=delayed):
            task = asyncio.create_task(self.service.execute(row))
            try:
                await self.wait_entered(entered)
                task.cancel()
                await asyncio.sleep(.02)
                self.assertFalse(task.done())
                close.assert_not_awaited()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                Agent.assert_not_called()
                close.assert_awaited_once_with(self.model)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(
            sorted(
                p.read_bytes()
                for p in (self.queue.sources / previous).iterdir()
            ), [b'OUTPUT', b'SOURCE'],
        )

    async def test_cancel_preserves_cancellation_if_preparation_also_fails(self):
        _, _, row = self.followup()
        entered, release = threading.Event(), threading.Event()

        def failing(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Event loop stalled')
            raise ValueError('private read failed')
        with self.agent_patch() as Agent, patch('company_agent.api.service.close_model', new=AsyncMock()) as close, \
                patch.object(self.queue.store, 'read_artifact', side_effect=failing):
            task = asyncio.create_task(self.service.execute(row))
            try:
                await self.wait_entered(entered)
                task.cancel()
                await asyncio.sleep(.02)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
                Agent.assert_not_called()
                close.assert_awaited_once_with(self.model)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


class FileToolResponsivenessTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_receipt_reservation_and_audit_leave_event_loop_responsive(self):
        for operation in ('begin_tool', 'finish_tool', 'audit'):
            with self.subTest(operation=operation):
                original = getattr(self.store, operation)
                entered, release = threading.Event(), threading.Event()

                def delayed(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('Receipt/audit blocked the event loop')
                    return original(*args, **kwargs)
                runtime = SimpleNamespace(
                    context=Context('alice', self.job, self.config, self.store),
                    state={}, tool_call_id=operation, stream_writer=lambda _: None,
                )
                with patch.object(self.store, operation, side_effect=delayed):
                    task = asyncio.create_task(
                        file_tool(
                            runtime, tool_name='create_text', input_schema=Text,
                            name=f'{operation}.txt', content='draft',
                        ),
                    )
                    try:
                        async with asyncio.timeout(5):
                            while not entered.is_set():
                                await asyncio.sleep(.01)
                        release.set()
                        result = await asyncio.wait_for(task, 5)
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                receipt = json.loads(result.update['messages'][0].content)
                self.assertEqual(receipt['name'], f'{operation}.txt')
                self.assertEqual(self.store.read_artifact('alice', receipt['id'], self.job)[2], b'draft')
