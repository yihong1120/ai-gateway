"""No live EIP writes: exercise durable opt-in automation with fake services."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch
from zoneinfo import ZoneInfo

import examples.official_document_workflow.infrastructure.eip.netask as _netask_imports
from examples.official_document_workflow.application import automatic_task
from examples.official_document_workflow.application import automation
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.domain.calendar.scheduling import EIP_REVIEW_NOTICE
from examples.official_document_workflow.domain.calendar.scheduling import TENTATIVE_NOTICE
from examples.official_document_workflow.domain.validation import ContentValidationError
from examples.official_document_workflow.infrastructure.eip.netask import Netask
from examples.official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.official_document_workflow.infrastructure.storage.state import State
from examples.official_document_workflow.runtime import current_resources
from examples.shared.runtime.modules import ModuleReference
from tests.eip_calendar_fixture import new_calendar_matches
from tests.eip_postgres import EIPPostgresTestCase

_fixture_test_eip_connector = ModuleReference('tests.test_eip_connector')


def document():
    return {
        'id': '10',
        'folder': 'inbox',
        'subject': '測試公文',
        'unread': True,
        'date': datetime.now(ZoneInfo('Asia/Taipei')).isoformat(),
    }


class AutomationTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.key = root / 'keys/key'
        self.db = AutomationStore(self.key)
        self.journal = Journal()
        self.db.bind('alice', '17', 'test-account', 'test-password')
        self.generation = self.db.account('alice')['generation']
        self.adapter = types.SimpleNamespace(
            user_id='17',
            login=AsyncMock(),
            close=AsyncMock(),
            automatic_documents=AsyncMock(return_value=[document()]),
            attachments=AsyncMock(return_value=[{'name': 'a.pdf', 'url': '/fo/a'}]),
            download=AsyncMock(return_value=b'%PDF-test'),
            stage=AsyncMock(return_value=[]),
            calendar_projects=AsyncMock(return_value=[]),
            commit=AsyncMock(return_value=None),
            calendar_import_matches=AsyncMock(side_effect=new_calendar_matches),
        )

        async def document_detail(*args):
            return {'fields': [], 'attachments': await self.adapter.attachments(*args)}

        self.adapter.document_detail = AsyncMock(side_effect=document_detail)
        self.output = {
            'complete': True,
            'files': [{'id': 'word-id', 'name': 'document.docx'}],
            'csv_ids': ['csv-id'],
            'has_deadlines': True,
            'message': '完成',
        }
        self.event = {'key': 'test-key', 'line': 'verified-import-line', 'row': {}}
        self.patches = [
            patch.object(current_resources(), 'state', State()),
            patch.object(
                integration,
                'source_file',
                AsyncMock(return_value=types.SimpleNamespace(id='source-id')),
            ),
            patch.object(integration, 'process', AsyncMock(return_value=self.output)),
            patch.object(
                integration, 'read_csv', AsyncMock(return_value=([{}], b'csv')),
            ),
            patch.object(
                automatic_task.calendar_sync,
                'verified_events',
                return_value=[self.event],
            ),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def run_once(self, verify=None, factory=None):
        # Run a complete scheduler iteration, including its account boundary.
        account = automation.AutomaticAccount(
            self.db, self.journal, self._workflow.state.processing,
            factory=factory or (lambda: self.adapter),
            verify=verify or AsyncMock(return_value=True),
        )
        scheduler = automation.AutomaticScheduler(
            self.db, self.journal, self._workflow.state,
            self._workflow.health['scheduler'], account,
        )
        with (
            patch.object(
                self.db, 'claim_due',
                return_value=[{'owner': 'alice', 'generation': self.generation}],
            ),
            patch.object(
                automation, 'asyncio', types.SimpleNamespace(
                    **{**vars(asyncio), 'sleep': AsyncMock(side_effect=asyncio.CancelledError)},
                ),
            ),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler.run()

    async def test_project_then_attachment_uses_verified_scope_and_no_replay(self):
        remote, row = _fixture_test_eip_connector.attributes('remote', 'row')

        line = _netask_imports.import_line(remote(row()), '17')
        self.event['line'] = line
        self.db.set_calendar_attach_renamed_pdf('alice', True)
        self.adapter.calendar_projects.return_value = [
            {'id': '77', 'name': '01-工程甲', 'members': ['Ru17', 'Ru18']},
        ]
        self.adapter.commit.return_value = {
            'event_id': 'test-event',
            'detail_verified': True,
        }
        self.event['row'] = {
            'Description': '辦理事項：會議\n工程名稱：工程甲\n日期：2026/09/08',
        }
        self.output['files'].append(
            {
                'id': 'pdf-id',
                'kind': 'document_pdf',
                'name': '工程甲.pdf',
                'group_index': 1,
            },
        )
        with (
            patch(
                'examples.official_document_workflow.infrastructure.eip.calendar_projects.assign_project',
                AsyncMock(
                    return_value={
                        'event_id': 'test-event',
                        'project_id': '77',
                        'sharing': ['Ru17', 'Ru18'],
                        'detail_verified': True,
                    },
                ),
            ) as assign,
            patch(
                'examples.official_document_workflow.infrastructure.eip.calendar_attachments.attach_event',
                AsyncMock(return_value={'attachment_verified': True}),
            ) as attach,
            patch.object(
                integration,
                'read_calendar_attachments',
                AsyncMock(return_value=[('工程甲.pdf', b'%PDF-test')]),
            ),
        ):
            await self.run_once()
            task = self.db.tasks('alice')[0]
            self.assertEqual(task['phase'], 'done')
            self.assertEqual(
                task['payload']['calendar_project_assignments']['assigned_events'], 1,
            )
            assign.assert_awaited_once_with(
                self.adapter,
                'test-event',
                line,
                '77',
                sharing=['Ru17', 'Ru18'],
            )
            self.assertEqual(
                attach.call_args.kwargs,
                {'project_id': '77', 'sharing': ['Ru17', 'Ru18']},
            )
            await self.run_once()
            self.assertEqual(assign.await_count, 1)
            self.assertEqual(attach.await_count, 1)

    async def test_attachment_preflight_failure_never_creates_calendar(self):
        self.db.set_calendar_attach_renamed_pdf('alice', True)
        self.output['files'].append(
            {
                'id': 'missing',
                'name': 'original.pdf',
                'kind': 'document_pdf',
                'group_index': 1,
            },
        )
        with patch.object(
            integration,
            'read_calendar_attachments',
            AsyncMock(side_effect=ValueError('附件不可讀')),
        ):
            await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        self.assertIsNone(self.journal.status('test-key'))

    async def test_enabled_attachment_without_pdf_is_not_silently_done(self):
        self.db.set_calendar_attach_renamed_pdf('alice', True)
        await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_inbox_due_date_without_attachment_event_creates_no_calendar(self):
        due_document = {
            **document(),
            'due_date': '2026-09-18 23:59:59.000',
            'flow_number': '2609110013',
        }
        self.adapter.automatic_documents.return_value = [due_document]
        self.output['csv_ids'] = []
        self.output['has_deadlines'] = False
        with patch.object(
            integration, 'calendar_file', AsyncMock(),
        ) as store_calendar:
            await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'done')
        self.assertFalse(task['payload']['output']['csv_ids'])
        store_calendar.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_due_date_does_not_modify_attachment_calendar(self):
        due_document = {
            **document(),
            'due_date': '2026-09-18 23:59:59.000',
            'flow_number': '2609110013',
        }
        self.adapter.automatic_documents.return_value = [due_document]
        original_file = {
            'id': 'csv-id',
            'name': '行事曆_附件組_1組.csv',
            'kind': 'calendar_csv',
        }
        self.output['files'].append(original_file)
        with patch.object(integration, 'calendar_file', AsyncMock()) as store_calendar:
            await self.run_once()
        output = self.db.tasks('alice')[0]['payload']['output']
        self.assertEqual(output['csv_ids'], ['csv-id'])
        self.assertEqual(
            [item for item in output['files'] if item.get('kind') == 'calendar_csv'],
            [original_file],
        )
        store_calendar.assert_not_awaited()
        self.assertEqual(self.adapter.stage.await_count, 1)

    async def test_due_date_cannot_bypass_incomplete_attachment_analysis(self):
        due_document = {
            **document(),
            'due_date': '2026-09-18 23:59:59.000',
            'flow_number': '2609110013',
        }
        self.db.discover('alice', '17', [due_document])
        task = self.db.tasks('alice')[0]
        payload = task['payload']
        payload['source_ids'] = ['source-id']
        payload['output'] = {
            **self.output,
            'complete': False,
            'csv_ids': [],
            'calendar_ready': False,
            'calendar_pending': ['文件內容日期待確認'],
        }
        self.db.save_task(task, 'converted', payload)
        self.adapter.automatic_documents.return_value = [due_document]
        with patch.object(
            integration, 'calendar_file', AsyncMock(),
        ) as store_calendar:
            await self.run_once()
        saved = self.db.tasks('alice')[0]
        self.assertEqual(saved['phase'], 'needs_review')
        self.assertNotIn('import', saved['payload'])
        store_calendar.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()
        integration.process.assert_not_awaited()

    async def test_existing_import_does_not_require_attachment_access(self):
        self.db.set_calendar_attach_renamed_pdf('alice', True)
        self.journal.reserve_event('test-key')
        self.journal.done('test-key')
        with patch.object(
            integration, 'read_calendar_attachments', AsyncMock(),
        ) as read:
            await self.run_once()
            read.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')

    def test_interrupted_project_edit_requires_review(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        self.db.save_task(task, 'assigning_projects', task['payload'])
        self.db.recover()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    def replacement(self):
        adapter = types.SimpleNamespace(
            user_id='17',
            login=AsyncMock(),
            close=AsyncMock(),
            automatic_documents=AsyncMock(return_value=[document()]),
            attachments=AsyncMock(return_value=[{'name': 'a.pdf', 'url': '/fo/a'}]),
            download=AsyncMock(return_value=b'%PDF-test'),
            stage=AsyncMock(return_value=[]),
            calendar_projects=AsyncMock(return_value=[]),
            commit=AsyncMock(return_value=None),
            calendar_import_matches=AsyncMock(side_effect=new_calendar_matches),
        )

        async def document_detail(*args):
            return {'fields': [], 'attachments': await adapter.attachments(*args)}

        adapter.document_detail = AsyncMock(side_effect=document_detail)
        return adapter

    async def test_hourly_run_starts_with_new_login_even_without_browser_session(self):
        first, second = self.replacement(), self.replacement()
        factory = Mock(side_effect=[first, second])
        await self.run_once(factory=factory)
        await self.run_once(factory=factory)
        for client in (first, second):
            client.login.assert_awaited_once_with('test-account', 'test-password')
            client.close.assert_awaited_once()
        first.commit.assert_awaited_once()
        second.commit.assert_not_awaited()

    async def test_temporary_cloud_failure_stops_without_scheduling_retry(self):
        integration.process.return_value = {
            'complete': False,
            'files': [],
            'csv_ids': [],
            'deferred_cloud_failure': True,
        }
        await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(task['retry_at'], 0)
        self.assertIn('不會自動重試', task['payload']['notice'])
        self.assertTrue(self.db.account('alice')['enabled'])
        self.db.recover()
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()

    async def test_cloud_429_stops_further_model_calls_in_same_hourly_run(self):
        docs = [document(), {**document(), 'id': '11'}]
        self.adapter.automatic_documents.return_value = docs
        integration.process.return_value = {
            'complete': False,
            'deferred_cloud_failure': True,
        }
        await self.run_once()
        integration.process.assert_awaited_once()
        self.assertEqual(
            {t['document_id']: t['phase'] for t in self.db.tasks('alice')},
            {'10': 'needs_review', '11': 'discovered'},
        )
        self.adapter.attachments.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()

    async def test_each_read_expiry_propagates_and_stops_without_relogin(self):
        for method in ('automatic_documents', 'attachments', 'download', 'stage'):
            with self.subTest(method=method):
                with self.db.db() as db:
                    db.execute('DELETE FROM tasks')
                adapter = self.replacement()
                failure = ConnectionError('expired')
                getattr(adapter, method).side_effect = failure
                factory = Mock(return_value=adapter)
                with self.assertRaises(ConnectionError) as caught:
                    await automation.AutomaticAccount(
                        self.db,
                        self.journal,
                        self._workflow.state.processing,
                        factory=factory,
                        verify=AsyncMock(return_value=True),
                    ).run('alice', self.generation)
                self.assertIs(caught.exception, failure)
                factory.assert_called_once()
                adapter.login.assert_awaited_once()
                getattr(adapter, method).assert_awaited_once()
                adapter.commit.assert_not_awaited()
                adapter.close.assert_awaited_once()
                tasks = self.db.tasks('alice')
                self.assertEqual(
                    [t['phase'] for t in tasks],
                    [] if method == 'automatic_documents' else ['needs_review'],
                )

    async def test_expiry_after_conversion_does_not_repeat_cloud_or_download(self):
        new = self.replacement()
        self.adapter.stage.side_effect = ConnectionError('expired')
        await self.run_once(factory=Mock(side_effect=[self.adapter, new]))
        integration.process.assert_awaited_once()
        integration.source_file.assert_awaited_once()
        self.adapter.download.assert_awaited_once()
        new.download.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        new.login.assert_not_awaited()

    async def test_repeated_expiry_stops_run_without_touching_other_tasks(self):
        new = self.replacement()
        self.adapter.automatic_documents.return_value = [
            document(),
            {**document(), 'id': '20'},
        ]
        self.adapter.attachments.side_effect = ConnectionError('expired')
        new.attachments.side_effect = ConnectionError('expired again')
        factory = Mock(side_effect=[self.adapter, new])
        await self.run_once(factory=factory)
        self.assertEqual(factory.call_count, 1)
        phases = {t['document_id']: t['phase'] for t in self.db.tasks('alice')}
        self.assertEqual(phases, {'10': 'needs_review', '20': 'discovered'})
        self.assertEqual(
            next(t for t in self.db.tasks('alice') if t['document_id'] == '10')[
                'retry_at'
            ],
            0,
        )
        integration.process.assert_not_awaited()
        new.commit.assert_not_awaited()

    async def test_generic_read_error_never_causes_relogin(self):
        self.adapter.download.side_effect = ValueError('permission or network error')
        factory = Mock(return_value=self.adapter)
        await self.run_once(factory=factory)
        factory.assert_called_once()
        self.adapter.login.assert_awaited_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_calendar_auth_error_is_not_relogged_or_replayed(self):
        self.adapter.commit.side_effect = ConnectionError('expired')
        factory = Mock(return_value=self.adapter)
        await self.run_once(factory=factory)
        factory.assert_called_once()
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(self.journal.status('test-key'), 'unknown')
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        await self.run_once()
        self.adapter.commit.assert_awaited_once()

    async def test_pause_during_expired_read_does_not_relogin(self):
        async def expired(*args):
            self.db.pause('alice')
            raise ConnectionError('expired')

        self.adapter.attachments.side_effect = expired
        factory = Mock(return_value=self.adapter)
        await self.run_once(factory=factory)
        factory.assert_called_once()
        self.assertFalse(self.db.account('alice')['enabled'])

    def test_encryption_at_rest_owner_binding_and_key_permissions(self):
        with self.db.db() as db:
            raw = b''.join(
                value
                for row in db.execute('SELECT secret FROM accounts')
                for value in row.values()
                if value
            )
        self.assertNotIn(b'test-password', raw)
        self.assertNotIn(b'test-account', raw)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('password', self.db.account('alice'))
        self.assertEqual(
            self.db.account('alice', secrets=True)['password'], 'test-password',
        )
        with self.assertRaises((ValueError, ValueError)):
            self.db.decode('bob', self.db.encode('alice', {}))

    def test_do_not_silently_replace_lost_key(self):
        self.key.unlink()
        with self.assertRaises(ValueError):
            AutomationStore(self.key)
        self.assertFalse(self.key.exists())

    def test_duplicate_eip_identity_blocked(self):
        with self.assertRaises(ValueError):
            self.db.bind('bob', '17', 'other', 'other')

    def test_hourly_claim_no_replay_of_missed_ticks(self):
        with self.db.db() as db:
            db.execute('UPDATE accounts SET next_run=0')
        now = time.time()
        self.assertEqual(len(self.db.claim_due()), 1)
        self.assertEqual(self.db.claim_due(), [])
        self.assertGreaterEqual(self.db.account('alice')['next_run'], now + 3600)
        second = AutomationStore(self.key)
        self.assertEqual(second.claim_due(), [])

    def test_fixed_origin_survives_pause_rebind_unlink_and_restart(self):
        original = self.db.account('alice')['scan_start']
        self.assertAlmostEqual(original, time.time() - 86400, delta=5)
        self.db.pause('alice')
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=time.time() + 7 * 86400,
        ):
            self.db.bind('alice', '17', 'test-account', 'new-password')
            self.assertEqual(self.db.account('alice')['scan_start'], original)
            self.db.pause('alice', forget=True)
            self.db.bind('alice', '17', 'test-account', 'new-password')
        restarted = AutomationStore(self.key)
        self.assertEqual(restarted.account('alice')['scan_start'], original)

    def test_other_identity_gets_independent_origin(self):
        old = self.db.account('alice')['scan_start']
        future = time.time() + 7 * 86400
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=future,
        ):
            self.db.bind('alice', '18', 'other-account', 'other-password')
            self.assertEqual(self.db.account('alice')['scan_start'], future - 86400)
            self.db.bind('alice', '17', 'test-account', 'new-password')
            self.assertEqual(self.db.account('alice')['scan_start'], old)

    async def test_old_queued_document_does_not_bypass_origin(self):
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'sign', 'date': '2001-01-01 10:00:00'},
        ]
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'outside_scope')
        self.adapter.attachments.assert_not_awaited()
        integration.process.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_old_pending_processed_once_across_restart_and_folder_move(self):
        old = {**document(), 'date': '2001-01-01 10:00:00', 'unread': False}
        self.adapter.automatic_documents.return_value = [old]
        await self.run_once()
        self.db = AutomationStore(self.key)
        await self.run_once()
        self.adapter.automatic_documents.return_value = [{**old, 'folder': 'sign'}]
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')
        integration.process.assert_awaited_once()
        self.adapter.download.assert_awaited_once()
        self.adapter.commit.assert_awaited_once()

    async def test_date_skipped_pending_reopens_but_results_and_review_do_not(self):
        old = {**document(), 'date': '2001-01-01 10:00:00'}
        docs = [{**old, 'id': str(i)} for i in range(4)]
        self.db.discover('alice', '17', docs)
        for task in self.db.tasks('alice'):
            payload = task['payload']
            if task['document_id'] == '1':
                payload['output'] = {'files': ['preserved']}
            self.db.save_task(
                task,
                (
                    'needs_review'
                    if task['document_id'] == '2'
                    else 'done' if task['document_id'] == '3' else 'outside_scope'
                ),
                payload,
            )
        self.adapter.automatic_documents.return_value = docs
        await self.run_once()
        phases = {t['document_id']: t['phase'] for t in self.db.tasks('alice')}
        self.assertEqual(
            phases,
            {'0': 'done', '1': 'outside_scope', '2': 'needs_review', '3': 'done'},
        )
        integration.process.assert_awaited_once()

    async def test_stale_old_inbox_row_not_in_snapshot_is_not_processed(self):
        self.db.discover('alice', '17', [{**document(), 'date': '2001-01-01 10:00:00'}])
        self.adapter.automatic_documents.return_value = []
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'outside_scope')
        self.adapter.attachments.assert_not_awaited()

    async def test_downtime_does_not_move_origin_forward(self):
        original = self.db.account('alice')['scan_start']
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=time.time() + 7 * 86400,
        ):
            await self.run_once()
        self.adapter.automatic_documents.assert_awaited_once_with(original)
        integration.process.assert_awaited_once()

    async def test_happy_path_auto_import_and_no_duplicate_next_hour(self):
        await self.run_once()
        await self.run_once()
        self.adapter.login.assert_awaited()
        integration.read_csv.assert_awaited_once_with(
            'alice', 'csv-id', default_duration_minutes=60,
        )
        self.adapter.commit.assert_awaited_once_with('verified-import-line')
        integration.process.assert_awaited_once_with(
            'alice', ['source-id'], '', None, groups=[['source-id']],
            date_context=automatic_task.ReceiptContext('10', '', None),
        )
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')
        self.assertEqual(self.journal.status('test-key'), 'done')

    async def test_server_receipt_context_is_loaded_from_the_same_flow(self):
        self.adapter.document_detail.return_value = {
            'fields': [
                {'label': '發文字號', 'value': '測試第115號'},
                {'label': '收文日期', 'value': '115年10月1日'},
            ],
            'attachments': [{'name': 'a.pdf', 'url': '/fo/a'}],
        }
        self.adapter.document_detail.side_effect = None
        await self.run_once()
        self.assertEqual(
            integration.process.await_args.kwargs['date_context'],
            automatic_task.ReceiptContext('10', '測試第115號', '2026-10-01'),
        )
        self.adapter.document_detail.assert_awaited_once_with('10', 'inbox')

    async def test_imported_relative_estimate_is_visible_in_automatic_history(self):
        self.output['date_estimates'] = [{'estimated': True, 'deadline': '2026-10-08'}]
        await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'done')
        self.assertIn('1 筆 AI 推估期限', task['payload']['notice'])

    async def test_relative_recheck_requires_an_actual_estimate_before_marking_done(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        retained = [{'id': 'existing-pdf', 'kind': 'document_pdf', 'name': 'document.pdf'}]
        p = {
            **task['payload'], 'relative_date_recheck': {
                'revision': 'eip-relative-date-estimate-v1', 'retained_exports': retained,
                'retained_sha256': {'existing-pdf': 'verified-hash'},
            },
        }
        self.db.save_task(task, 'discovered', p)
        with patch.object(integration, 'retained_document_exports', AsyncMock(return_value=retained)):
            await self.run_once()
        current = self.db.tasks('alice')[0]
        self.assertEqual(current['phase'], 'needs_review')
        self.assertFalse(current['payload']['output']['complete'])
        self.assertIn(retained[0], current['payload']['output']['files'])
        self.assertTrue(integration.process.await_args.kwargs['calendar_only'])
        self.adapter.commit.assert_not_awaited()

    async def test_ai_tentative_import_reports_eip_review_and_does_not_replay(self):

        integration.read_csv.return_value = ([{'Description': TENTATIVE_NOTICE}], b'csv')
        await self.run_once()
        await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'done')
        self.assertIn('AI 暫排', task['payload']['notice'])
        self.assertIn(EIP_REVIEW_NOTICE, task['payload']['notice'])
        self.adapter.commit.assert_awaited_once_with('verified-import-line')

    async def test_discovery_checkpoint_precedes_detail_read_side_effect(self):
        async def detail(*args):
            self.assertEqual(self.db.tasks('alice')[0]['phase'], 'downloading')
            return [{'name': 'a.pdf', 'url': '/fo/a'}]

        self.adapter.attachments.side_effect = detail
        await self.run_once()

    async def test_already_read_pending_document_processed_once(self):
        self.adapter.automatic_documents.return_value = [
            {**document(), 'unread': False},
        ]
        await self.run_once()
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.attachments.assert_awaited_once()
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')

    async def test_read_state_change_does_not_reprocess_completed_document(self):
        await self.run_once()
        self.adapter.automatic_documents.return_value = [
            {**document(), 'unread': False},
        ]
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_awaited_once()

    async def test_no_dates_does_not_invent_calendar(self):
        integration.process.return_value = {
            **self.output,
            'csv_ids': [],
            'has_deadlines': False,
        }
        await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')

    async def test_undated_event_preserves_document_exports_for_dashboard(self):
        exports = [
            {'id': 'pdf-id', 'name': '重新命名.pdf', 'kind': 'document_pdf'},
            {'id': 'word-id', 'name': '重新命名.docx', 'kind': 'document_docx'},
        ]
        integration.process.return_value = {
            **self.output,
            'complete': False,
            'files': exports,
            'csv_ids': [],
            'has_deadlines': False,
            'calendar_status': 'needs_review',
            'calendar_pending': ['行程 1 日期待補'],
            'errors': ['行程 1 日期待補'],
        }
        await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(task['payload']['output']['files'], exports)
        self.assertIn('已確認完成的檔案已保留供下載', task['payload']['notice'])
        self.adapter.commit.assert_not_awaited()

    async def test_undated_event_ignores_eip_due_date_and_keeps_exports(self):
        due_document = {
            **document(),
            'due_date': '2026-09-18 23:59:59.000',
            'flow_number': '2609110012',
        }
        self.adapter.automatic_documents.return_value = [due_document]
        integration.process.return_value = {
            **self.output,
            'complete': False,
            'files': [{'id': 'pdf-id', 'name': '重新命名.pdf', 'kind': 'document_pdf'}],
            'csv_ids': [],
            'has_deadlines': False,
            'calendar_status': 'needs_review',
            'calendar_pending': ['行程 1 日期待補'],
            'errors': ['行程 1 日期待補'],
        }
        with patch.object(integration, 'calendar_file', AsyncMock()) as store_calendar:
            await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertNotIn('import', task['payload'])
        self.assertEqual(
            task['payload']['output']['files'],
            [{'id': 'pdf-id', 'name': '重新命名.pdf', 'kind': 'document_pdf'}],
        )
        store_calendar.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_no_pdf_not_treated_as_document(self):
        self.adapter.attachments.return_value = []
        await self.run_once()
        await self.run_once()
        self.adapter.commit.assert_not_awaited()
        integration.process.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'no_pdf')
        self.adapter.attachments.assert_awaited_once()

    async def test_no_attachment_rechecked_after_hour_in_current_folder(self):
        self.adapter.attachments.return_value = []
        await self.run_once()
        before = self.db.account('alice')['scan_start']
        task = self.db.tasks('alice')[0]
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'sign'},
        ]
        self.adapter.attachments.return_value = [
            {'name': 'added.docx', 'url': '/fo/new'},
        ]
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=task['updated'] + 3601,
        ):
            await self.run_once()
        self.adapter.attachments.assert_awaited_with('10', 'sign')
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')
        self.assertEqual(self.db.account('alice')['scan_start'], before)
        await self.run_once()
        self.adapter.commit.assert_awaited_once()

    async def test_empty_recheck_does_not_spend_or_write(self):
        self.adapter.attachments.return_value = []
        await self.run_once()
        checked = self.db.tasks('alice')[0]['updated']
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=checked + 3601,
        ):
            await self.run_once()
        self.assertEqual(self.adapter.attachments.await_count, 2)
        integration.process.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'no_pdf_final')

    async def test_empty_attachment_is_not_scanned_again_after_bounded_recheck(self):
        self.adapter.attachments.return_value = []
        await self.run_once()
        checked = self.db.tasks('alice')[0]['updated']
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=checked + 3601,
        ):
            await self.run_once()
        self.assertEqual(self.adapter.attachments.await_count, 2)
        # A later hourly inbox scan sees the same document but must not read its
        # attachment list a third time.  New attachments require manual restart.
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=checked + 7202,
        ):
            await self.run_once()
        self.assertEqual(self.adapter.attachments.await_count, 2)
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'no_pdf_final')

    async def test_no_attachment_recheck_requires_rediscovery(self):
        self.adapter.attachments.return_value = []
        await self.run_once()
        checked = self.db.tasks('alice')[0]['updated']
        self.adapter.automatic_documents.return_value = []
        with patch(
            'examples.official_document_workflow.infrastructure.storage.automation_store.time.time',
            return_value=checked + 3601,
        ):
            await self.run_once()
        self.adapter.attachments.assert_awaited_once()

    async def test_moved_queued_document_uses_latest_folder(self):
        self.db.discover('alice', '17', [document()])
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'notify'},
        ]
        await self.run_once()
        self.adapter.attachments.assert_awaited_once_with('10', 'notify')

    async def test_sent_document_processed_once_then_moved_without_reimport(self):
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'sent'},
        ]
        original = self.db.account('alice')['scan_start']
        await self.run_once()
        self.adapter.attachments.assert_awaited_once_with('10', 'sent')
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'sign'},
        ]
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(self.db.account('alice')['scan_start'], original)

    async def test_sent_without_document_deadline_does_not_import(self):
        self.adapter.automatic_documents.return_value = [
            {**document(), 'folder': 'sent'},
        ]
        integration.process.return_value = {
            **self.output,
            'csv_ids': [],
            'has_deadlines': False,
        }
        await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')

    def test_rediscovery_never_resets_terminal_or_ambiguous_phases(self):
        for phase in (
            'done',
            'needs_review',
            'importing',
            'processing',
            'downloading',
            'outside_scope',
        ):
            doc = {**document(), 'id': phase}
            self.db.discover('alice', '17', [doc])
            task = next(t for t in self.db.tasks('alice') if t['document_id'] == phase)
            payload = {**task['payload'], 'output': {'files': ['preserve']}}
            self.db.save_task(task, phase, payload)
            self.db.discover('alice', '17', [{**doc, 'folder': 'sign'}])
            after = next(t for t in self.db.tasks('alice') if t['document_id'] == phase)
            self.assertEqual(after['phase'], phase)
            self.assertEqual(after['payload'], payload)

    async def test_partial_scan_failure_does_not_download_or_import(self):
        self.adapter.automatic_documents.side_effect = ValueError('scan failed')
        await self.run_once()
        self.assertEqual(self.db.tasks('alice'), [])
        self.adapter.attachments.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_conversion_failure_keeps_files_but_no_auto_retry(self):
        integration.process.return_value = {**self.output, 'complete': False}
        await self.run_once()
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(task['payload']['output']['files'], self.output['files'])

    async def test_unknown_calendar_write_is_never_retried_after_restart(self):
        self.adapter.commit.side_effect = ValueError('timeout')
        await self.run_once()
        self.db.recover()
        await self.run_once()
        self.assertEqual(self.journal.status('test-key'), 'unknown')
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_incomplete_document_stops_even_when_calendar_is_ready(self):
        confirmed = [{'id': 'pdf-id', 'name': 'document.pdf', 'kind': 'document_pdf'}]
        integration.process.return_value = {
            **self.output,
            'complete': False,
            'status': 'partial',
            'files': confirmed,
            'calendar_ready': True,
            'errors': ['第 1 組：DOCX 結果不明，請核對'],
        }
        await self.run_once()
        self.db.recover()
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertTrue(task['payload']['output']['errors'])
        self.assertEqual(task['payload']['output']['files'], confirmed)
        self.assertNotIn('文件轉檔已完成', task['payload']['notice'])
        self.assertIn('不會自動重送', task['payload']['notice'])

    async def test_verified_partial_calendar_imports_once_and_preserves_pending_items(self):
        integration.process.return_value = {
            **self.output,
            'complete': False,
            'calendar_ready': True,
            'calendar_pending': ['行程 2 日期待補'],
            'errors': ['行程 2 日期待補'],
        }
        await self.run_once()
        await self.run_once()
        self.adapter.commit.assert_awaited_once()
        integration.process.assert_awaited_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(
            task['payload']['output']['calendar_pending'], ['行程 2 日期待補'],
        )
        self.assertEqual(task['payload']['import'], {'created': 1, 'skipped': 0})
        self.assertIn('其餘項目保留待核對', task['payload']['notice'])

    async def test_partial_calendar_with_unrelated_document_error_still_stops(self):
        integration.process.return_value = {
            **self.output, 'complete': False, 'calendar_ready': True,
            'calendar_pending': ['行程 2 日期待補'],
            'errors': ['行程 2 日期待補', 'DOCX failed'],
        }
        await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_empty_partial_calendar_is_not_marked_as_imported(self):
        integration.process.return_value = {
            **self.output, 'complete': False, 'calendar_ready': True,
            'calendar_pending': ['行程 2 日期待補'], 'errors': ['行程 2 日期待補'],
        }
        with patch.object(automatic_task.calendar_sync, 'verified_events', return_value=[]):
            await self.run_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_tracking_reminders_are_passed_only_for_authorized_binding(self):
        with patch.dict(os.environ, {'EIP_TRACKING_REMINDER_ACCOUNTS': 'alice:17'}):
            await self.run_once()
        self.assertTrue(integration.process.await_args.kwargs['tracking_reminders'])

    def legacy_calendar_gate_task(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        payload = {
            **task['payload'],
            'source_ids': ['source-id'],
            'output': {
                **self.output,
                'complete': False,
                'calendar_ready': True,
                'calendar_status': 'ready',
                'errors': [
                    '第 1 組：公文改名待核對：cloud required field subject lacks source evidence',
                ],
                'warnings': [],
            },
            'last_error': {'stage': 'processing', 'type': 'ValueError'},
            'notice': '公文尚未完整產生 PDF／Word／CSV，已保留成功輸出，未自動匯入。',
        }
        self.db.save_task(task, 'needs_review', payload)
        return task, payload

    def legacy_preview_task(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        p = {
            **task['payload'],
            'source_ids': ['source-id'],
            'output': dict(self.output),
            'last_error': {'stage': 'calendar_preview', 'type': 'ValueError'},
            'notice': 'EIP 解析主旨或日期與 CSV 不符，已停止匯入。',
        }
        self.db.save_task(task, 'needs_review', p)
        return task, p

    def date_provenance_task(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        error = 'CSV 未完成：行程 2 的 start_date 與所引原文不符或日期格式待確認。'
        p = {
            **task['payload'],
            'source_ids': ['source-id'],
            'output': {
                'complete': False,
                'files': [
                    {'id': 'old-word', 'name': 'document.docx', 'kind': 'document_docx'},
                ],
                'csv_ids': [],
                'errors': ['第 1 組：' + error],
                'calendar_errors': [error],
                'warnings': [],
                'calendar_ready': False,
                'calendar_status': 'needs_review',
            },
            'last_error': {'stage': 'processing', 'type': 'ValueError'},
            'notice': '行事曆尚未通過來源驗證；既有文件已保留。',
        }
        self.db.save_task(task, 'needs_review', p)
        return task, p

    async def test_retired_recoveries_do_not_reopen_review_tasks(self):
        for create in (
            self.date_provenance_task,
            self.legacy_preview_task,
            self.legacy_calendar_gate_task,
        ):
            with self.subTest(case=create.__name__):
                _, original = create()
                await self.run_once()
                await self.run_once()
                task = self.db.tasks('alice')[0]
                self.assertEqual(task['phase'], 'needs_review')
                self.assertEqual(task['payload'], original)
                integration.process.assert_not_awaited()
                integration.source_file.assert_not_awaited()
                self.adapter.stage.assert_not_awaited()
                self.adapter.commit.assert_not_awaited()

    async def test_pause_during_conversion_prevents_import(self):
        async def process(owner, ids, instruction, emitter, *, groups, date_context=None):
            self.assertEqual(groups, [ids])
            self.assertEqual(instruction, '')
            self.db.pause('alice')
            return self.output

        integration.process.side_effect = process
        await self.run_once()
        self.adapter.commit.assert_not_awaited()

    async def test_deleted_or_pending_webui_user_cannot_run(self):
        await self.run_once(AsyncMock(return_value=False))
        self.adapter.login.assert_not_awaited()
        self.assertFalse(self.db.account('alice')['enabled'])

    async def test_rebound_generation_cannot_execute_old_job(self):
        self.db.bind('alice', '17', 'test-account', 'new-password')
        await self.run_once()
        self.adapter.login.assert_not_awaited()

    async def test_login_identity_mismatch_never_reads_documents(self):
        self.adapter.user_id = '99'
        await self.run_once()
        self.adapter.automatic_documents.assert_not_awaited()

    async def test_repeated_account_failure_keeps_hourly_schedule_without_password_logging(
        self,
    ):
        self.adapter.login.side_effect = ValueError('login failed')
        for _ in range(5):
            await self.run_once()
        self.assertTrue(self.db.account('alice')['enabled'])
        self.assertEqual(self.db.account('alice')['failures'], 5)
        self.assertNotIn('test-password', self.db.account('alice')['message'])

    async def test_legacy_converted_partial_output_cannot_bypass_fail_fast(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        payload = {
            **task['payload'],
            'output': {**self.output, 'complete': False, 'calendar_ready': True},
        }
        self.db.save_task(task, 'converted', payload)
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        self.adapter.stage.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_cancel_propagates_and_closes_session_without_retry(self):
        self.adapter.download.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await automation.AutomaticAccount(
                self.db,
                self.journal,
                self._workflow.state.processing,
                factory=lambda: self.adapter,
                verify=AsyncMock(return_value=True),
            ).run('alice', self.generation)
        self.adapter.close.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.db.account('alice')['failures'], 0)
        self.db.recover()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')

    async def test_legacy_retry_checkpoint_is_retired_without_replay(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        with self.db.db() as db:
            db.execute(
                "UPDATE tasks SET phase='downloaded',retry_at=%s WHERE id=%s",
                (time.time() - 1, task['id']),
            )
        self.assertEqual(self.db.tasks('alice', pending=True), [])
        self.db.recover()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(task['retry_at'], 0)
        await self.run_once()
        integration.process.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_first_bad_document_stops_run_and_next_hour_handles_only_remaining_document(
        self,
    ):
        self.adapter.automatic_documents.return_value = [
            document(),
            {**document(), 'id': '20'},
        ]
        integration.process.side_effect = [
            RuntimeError('test-password hidden body'),
            self.output,
        ]
        await self.run_once()
        phases = {t['document_id']: t['phase'] for t in self.db.tasks('alice')}
        self.assertEqual(phases, {'10': 'needs_review', '20': 'discovered'})
        self.assertEqual(self.db.account('alice')['failures'], 1)
        self.assertTrue(self.db.account('alice')['enabled'])
        bad = next(t for t in self.db.tasks('alice') if t['document_id'] == '10')
        self.assertEqual(bad['payload']['last_error']['stage'], 'processing')
        self.assertEqual(bad['payload']['last_error']['site']['function'], 'analyze')
        self.assertEqual(
            set(bad['payload']['last_error']['site']), {'module', 'function', 'line'},
        )
        self.assertNotIn('test-password', str(bad))
        integration.process.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()
        await self.run_once()
        self.assertEqual(
            {t['document_id']: t['phase'] for t in self.db.tasks('alice')},
            {'10': 'needs_review', '20': 'done'},
        )
        self.assertEqual(integration.process.await_count, 2)

    async def test_attachment_limit_marks_only_that_document_for_review_and_continues(self):
        self.adapter.automatic_documents.return_value = [document(), {**document(), 'id': '20'}]
        self.adapter.attachments.side_effect = [
            [{'name': f'{index}.pdf', 'url': f'/fo/{index}'} for index in range(11)],
            [{'name': 'valid.pdf', 'url': '/fo/valid'}],
        ]
        await self.run_once()
        tasks = {task['document_id']: task for task in self.db.tasks('alice')}
        self.assertEqual(tasks['10']['phase'], 'needs_review')
        self.assertEqual(
            tasks['10']['payload']['input_issue'], {
                'code': 'too_many_attachments', 'attachment_count': 11, 'max_attachments': 10,
            },
        )
        self.assertNotIn('source_ids', tasks['10']['payload'])
        self.assertNotIn('last_error', tasks['10']['payload'])
        self.assertEqual(tasks['20']['phase'], 'done')
        self.assertEqual(self.db.account('alice')['failures'], 0)
        self.adapter.download.assert_awaited_once_with('/fo/valid', 'valid.pdf')
        integration.process.assert_awaited_once()
        await self.run_once()
        self.assertEqual(self.adapter.attachments.await_count, 2)
        integration.process.assert_awaited_once()

    async def test_exactly_ten_attachments_are_processed(self):
        self.adapter.attachments.return_value = [
            {'name': f'{index}.pdf', 'url': f'/fo/{index}'} for index in range(10)
        ]
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'done')
        self.assertEqual(self.adapter.download.await_count, 10)
        integration.process.assert_awaited_once()

    async def test_document_validation_failure_preserves_sources_and_continues(self):
        self.adapter.automatic_documents.return_value = [document(), {**document(), 'id': '20'}]
        integration.process.side_effect = [integration.DocumentValidationError('private response'), self.output]
        await self.run_once()
        tasks = {task['document_id']: task for task in self.db.tasks('alice')}
        self.assertEqual(tasks['10']['phase'], 'needs_review')
        self.assertEqual(tasks['10']['payload']['input_issue']['code'], 'document_validation_failed')
        self.assertTrue(tasks['10']['payload']['source_ids'])
        self.assertNotIn('output', tasks['10']['payload'])
        self.assertNotIn('import', tasks['10']['payload'])
        self.assertNotIn('private response', str(tasks['10']))
        self.assertEqual(tasks['20']['phase'], 'done')
        self.assertEqual(self.db.account('alice')['failures'], 0)
        self.assertEqual(self.adapter.commit.await_count, 1)
        await self.run_once()
        self.assertEqual(integration.process.await_count, 2)

    async def test_classified_content_failure_shows_resolution_and_continues(self):
        issue = ContentValidationError('document_group_ambiguous', group_index=1).as_issue()
        self.adapter.automatic_documents.return_value = [document(), {**document(), 'id': '20'}]
        integration.process.side_effect = [
            integration.DocumentValidationError(
                'ignored private output', issue=issue,
            ), self.output,
        ]
        await self.run_once()
        tasks = {t['document_id']: t for t in self.db.tasks('alice')}
        self.assertEqual(tasks['10']['phase'], 'needs_review')
        self.assertEqual(tasks['10']['payload']['input_issue'], issue)
        self.assertIn('分組選取', tasks['10']['payload']['notice'])
        self.assertNotIn('ignored private output', str(tasks['10']))
        self.assertEqual(tasks['20']['phase'], 'done')
        self.assertEqual(self.db.account('alice')['failures'], 0)

    async def test_download_failure_does_not_resume_saved_attachments(self):
        self.adapter.attachments.return_value = [
            {'name': 'a.pdf', 'url': '/fo/a'},
            {'name': 'b.pdf', 'url': '/fo/b'},
        ]
        self.adapter.download.side_effect = [b'%PDF-a', TimeoutError('network')]
        await self.run_once()
        task = self.db.tasks('alice')[0]
        self.assertEqual(task['phase'], 'needs_review')
        self.assertEqual(task['retry_at'], 0)
        self.assertTrue(task['payload']['source_ids'])
        self.db.recover()
        await self.run_once()
        self.assertEqual(self.adapter.download.await_count, 2)
        integration.process.assert_not_awaited()
        self.adapter.commit.assert_not_awaited()

    async def test_preview_failure_does_not_retry_or_import(self):
        self.adapter.stage.side_effect = TimeoutError('network')
        await self.run_once()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        self.assertIsNone(self.journal.status('test-key'))
        await self.run_once()
        integration.process.assert_awaited_once()
        self.adapter.stage.assert_awaited_once()
        self.adapter.commit.assert_not_awaited()

    def test_stale_finish_cannot_override_manual_pause_or_new_binding(self):
        self.db.pause('alice')
        self.db.finish('alice', self.generation, 'old worker failure', failed=True)
        self.assertEqual(self.db.account('alice')['message'], '已暫停')
        self.db.bind('alice', '17', 'test-account', 'new-password')
        self.db.finish('alice', self.generation, 'old worker failure', failed=True)
        self.assertEqual(self.db.account('alice')['failures'], 0)
        self.assertTrue(self.db.account('alice')['enabled'])

    def test_forget_credentials_preserves_work_and_import_journal(self):
        self.db.discover('alice', '17', [document()])
        self.journal.reserve('known')
        self.db.pause('alice', forget=True)
        self.assertIsNone(self.db.account('alice', secrets=True))
        self.assertEqual(len(self.db.tasks('alice')), 1)
        self.assertEqual(self.journal.status('known'), 'unknown')

    def test_interrupted_conversion_becomes_review(self):
        self.db.discover('alice', '17', [document()])
        task = self.db.tasks('alice')[0]
        self.db.save_task(task, 'processing', task['payload'])
        self.db.recover()
        self.assertEqual(self.db.tasks('alice')[0]['phase'], 'needs_review')
        self.assertEqual(self.db.tasks('alice', '17', pending=True), [])


class PendingInboxTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_four_folders_dedupe_dates_and_pagination(self):
        n = Netask()
        try:
            cutoff = time.time() - 86400
            first = [{**document(), 'id': str(i)} for i in range(20)]

            async def pages(folder, start, end, page):
                if folder == 'inbox':
                    return first if page == 0 else [{**document(), 'id': '20'}]
                if folder == 'sign':
                    return [first[0], {**document(), 'id': '21'}]
                if folder == 'sent':
                    return [first[0], {**document(), 'id': '23'}]
                return [
                    {**document(), 'id': '21'},
                    {**document(), 'id': '22'},
                    {**document(), 'id': 'old', 'date': '2001-01-01 10:00:00'},
                ]

            n.documents = AsyncMock(side_effect=pages)
            result = await n.automatic_documents(cutoff)
            self.assertEqual(len(result), 24)
            self.assertEqual(
                [(c.args[0], c.args[3]) for c in n.documents.call_args_list],
                [('inbox', 0), ('inbox', 1), ('sign', 0), ('notify', 0), ('sent', 0)],
            )
            by_id = {r['id']: r for r in result}
            self.assertEqual(by_id['0']['folder'], 'inbox')
            self.assertEqual(by_id['21']['folder'], 'sign')
            self.assertEqual(by_id['22']['folder'], 'notify')
            self.assertEqual(by_id['23']['folder'], 'sent')
        finally:
            await n.close()

    async def test_sent_rpc_mapping_uses_sent_time_not_planned_end_or_unread(self):
        n = Netask()
        try:
            row = [''] * 26
            row[0] = '10'
            row[3] = '送出文件'
            row[7] = '2001-01-01 10:00:00'
            row[8] = '2099-12-31 10:00:00'
            row[9] = '1'
            row[10] = 'S'
            n.rpc = AsyncMock(return_value=[[0, ''], row])
            docs = await n.documents('sent', '2001-01-01', '2099-12-31')
            self.assertEqual(n.rpc.call_args.kwargs['curfolder'], 'sent')
            self.assertEqual(n.rpc.call_args.kwargs['folder_id'], '0')
            self.assertEqual(docs[0]['date'], row[7])
            self.assertIsNone(docs[0]['unread'])
            self.assertFalse(n.received_since(docs[0], time.time() - 86400))
            self.assertNotIn('2099', str(docs))
            for unsupported in ('send', 'draft', 'applicant', 'in_progress', 'archive'):
                with self.assertRaises(ValueError):
                    await n.documents(unsupported, '2026-09-01', '2026-09-06')
            n.rpc.assert_awaited_once()
        finally:
            await n.close()

    async def test_sent_failure_stops_entire_snapshot(self):
        n = Netask()
        try:
            n.documents = AsyncMock(
                side_effect=[[document()], [], [], ValueError('sent failed')],
            )
            with self.assertRaises(ValueError):
                await n.automatic_documents(time.time() - 86400)
        finally:
            await n.close()

    async def test_failed_folder_does_not_return_partial_snapshot(self):
        n = Netask()
        try:
            n.documents = AsyncMock(
                side_effect=[[document()], ValueError('sign failed')],
            )
            with self.assertRaises(ValueError):
                await n.automatic_documents(time.time() - 86400)
        finally:
            await n.close()

    async def test_repeated_page_and_page_limit_are_explicit_errors(self):
        n = Netask()
        try:
            n.documents = AsyncMock(
                return_value=[{**document(), 'id': str(i)} for i in range(20)],
            )
            with self.assertRaisesRegex(ValueError, '分頁重複'):
                await n.automatic_documents(time.time() - 86400)

            async def pages(folder, start, end, page):
                return [{**document(), 'id': str(page * 20 + i)} for i in range(20)]

            n.documents = AsyncMock(side_effect=pages)
            with self.assertRaisesRegex(ValueError, '1000'):
                await n.automatic_documents(time.time() - 86400)
        finally:
            await n.close()

    async def test_exact_boundary_and_date_query(self):
        n = Netask()
        try:
            cutoff = datetime.fromisoformat('2026-09-04T15:30:00+08:00').timestamp()
            n.documents = AsyncMock(
                return_value=[
                    {**document(), 'id': '1', 'date': '2026/09/04 15:29:59'},
                    {**document(), 'id': '2', 'date': '2026/09/04 15:30:00'},
                    {**document(), 'id': '3', 'date': '2026-09-04T07:30:01+00:00'},
                ],
            )
            result = await n.automatic_documents(cutoff, ('sign',))
            self.assertEqual([r['id'] for r in result], ['2', '3'])
            self.assertEqual(n.documents.call_args.args[1], '2026-09-04')
        finally:
            await n.close()

    async def test_invalid_or_date_only_receipt_timestamp_stops_before_detail(self):
        n = Netask()
        try:
            n.attachments = AsyncMock()
            for raw in ['', None, 'not-a-date', '2026-09-04']:
                n.documents = AsyncMock(return_value=[{**document(), 'date': raw}])
                with self.assertRaises((ValueError, ValueError)):
                    await n.automatic_documents(time.time() - 86400, ('sign',))
            n.attachments.assert_not_awaited()
        finally:
            await n.close()

    async def test_inbox_has_no_date_filter_even_with_missing_metadata(self):
        n = Netask()
        try:
            n.documents = AsyncMock(
                return_value=[
                    {**document(), 'date': '2001-01-01'},
                    {**document(), 'id': '20', 'date': None},
                ],
            )
            result = await n.automatic_documents(
                time.time() - 86400, folders=('inbox',),
            )
            self.assertEqual(len(result), 2)
            n.documents.assert_awaited_once_with('inbox', None, None, 0)
        finally:
            await n.close()

    async def test_no_date_rpc_only_allowed_for_inbox(self):
        n = Netask()
        try:
            n.rpc = AsyncMock(return_value=[[0, '']])
            await n.documents('inbox', None, None)
            self.assertEqual(n.rpc.call_args.kwargs['start_date'], '')
            self.assertEqual(n.rpc.call_args.kwargs['end_date'], '')
            for folder in ('sign', 'sent', 'notify'):
                with self.assertRaises(ValueError):
                    await n.documents(folder, None, None)
            n.rpc.assert_awaited_once()
        finally:
            await n.close()

    async def test_both_read_and_unread_rows_selected_without_opening_detail(self):
        n = Netask()
        try:
            n.documents = AsyncMock(
                return_value=[
                    {**document(), 'unread': False},
                    {**document(), 'id': '20'},
                ],
            )
            n.attachments = AsyncMock()
            self.assertEqual(
                [
                    r['id']
                    for r in await n.automatic_documents(
                        time.time() - 86400, folders=('inbox',),
                    )
                ],
                ['10', '20'],
            )
            self.assertEqual(n.documents.call_args.args[0], 'inbox')
            n.attachments.assert_not_awaited()
        finally:
            await n.close()

    async def test_unknown_read_flag_does_not_exclude_pending_document(self):
        n = Netask()
        try:
            n.documents = AsyncMock(return_value=[{**document(), 'unread': None}])
            self.assertEqual(
                [
                    r['id']
                    for r in await n.automatic_documents(
                        time.time() - 86400, folders=('inbox',),
                    )
                ],
                ['10'],
            )
        finally:
            await n.close()

    async def test_all_pages_snapshot_and_duplicate_ids_deduped(self):
        n = Netask()
        try:
            first = [{**document(), 'id': str(i), 'unread': False} for i in range(20)]
            n.documents = AsyncMock(
                side_effect=[first, [first[0], {**document(), 'id': '20'}]],
            )
            result = await n.automatic_documents(
                time.time() - 86400, folders=('inbox',),
            )
            self.assertEqual(len(result), 21)
            self.assertEqual([c.args[3] for c in n.documents.call_args_list], [0, 1])
        finally:
            await n.close()

    async def test_mapping_subject_and_read_flag(self):
        n = Netask()
        try:
            r = [''] * 26
            r[0] = '10'
            r[3] = '正式主旨'
            r[7] = '2026-09-05'
            r[9] = '1'
            r[10] = 'S'
            n.rpc = AsyncMock(return_value=[[0, ''], r])
            result = await n.documents('inbox', '2026-01-01', '2026-09-05')
            self.assertEqual(result[0]['subject'], '正式主旨')
            self.assertTrue(result[0]['unread'])
        finally:
            await n.close()

    async def test_inbox_mapping_preserves_verified_due_date_and_flow_number(self):
        n = Netask()
        try:
            row = [''] * 26
            row[0] = '59290'
            row[3] = '緩撞車裝設紅藍LED桿式警示燈條案'
            row[7] = '2026-09-11 15:14:19.057'
            row[8] = '2026-09-18 23:59:59.000'
            row[9] = '0'
            row[10] = 'S'
            row[18] = '2609110013'
            n.rpc = AsyncMock(return_value=[[0, ''], row])
            result = await n.documents('inbox', None, None)
            self.assertEqual(result[0]['due_date'], row[8])
            self.assertEqual(result[0]['flow_number'], row[18])
        finally:
            await n.close()


class IntegrationValidationClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, error):
        class InvalidDocument(RuntimeError):
            pass

        failure = InvalidDocument('private model output') if error == 'validation' else error
        module = types.SimpleNamespace(CloudExtractionError=InvalidDocument)
        pipe = types.SimpleNamespace(valves=types.SimpleNamespace(document_analysis_cache_enabled=False))
        with patch.object(integration, 'configured_pipe', AsyncMock(return_value=pipe)), \
                patch.object(integration, 'module', new=module), \
                patch.object(integration, 'store', return_value=None), \
                patch.object(integration, 'process_with_team', AsyncMock(side_effect=failure)):
            return await integration.process('alice', ['source'], '', None, groups=[['source']])

    async def test_only_document_validation_is_classified_as_input_issue(self):
        with self.assertRaises(integration.DocumentValidationError) as caught:
            await self.invoke('validation')
        self.assertNotIn('private model output', str(caught.exception))

    async def test_structured_content_issue_survives_integration_boundary(self):
        error = ContentValidationError('document_metadata_unverified', group_index=1, repair_attempted=True)
        with self.assertRaises(integration.DocumentValidationError) as caught:
            await self.invoke(error)
        self.assertEqual(caught.exception.issue, error.as_issue())

    async def test_connection_timeout_and_unexpected_errors_keep_stop_semantics(self):
        for error in (ConnectionError('offline'), TimeoutError('busy'), RuntimeError('unexpected'), ValueError('invalid')):
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                await self.invoke(error)
            self.assertIs(caught.exception, error)


if __name__ == '__main__':
    unittest.main()
