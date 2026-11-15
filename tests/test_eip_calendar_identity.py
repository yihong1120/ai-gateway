"""Replay must survive rewrites, concurrency, legacy receipts and user isolation."""
from __future__ import annotations

from tests.eip_calendar_fixture import new_calendar_matches

import asyncio
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from unittest.mock import patch

from .test_eip_connector import remote
from .test_eip_connector import row
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.application.integration import annotate_csv_sources
from examples.official_document_workflow.infrastructure.eip.calendar_identity import prepare_source_replay
from examples.official_document_workflow.infrastructure.eip.calendar_sync import verified_events
from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase


class SourceReplayTests(EIPPostgresTestCase, IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.journal = Journal()
        self.adapter = SimpleNamespace(commit=AsyncMock(return_value={'event_id': '123', 'detail_verified': True}), calendar_import_matches=AsyncMock(side_effect=new_calendar_matches))

    def events(self, title='原名稱', user='17', day='2026/09/10', prior=()):
        r = {
            **row(), 'Subject': title, 'Start Date': day, 'End Date': day,
            '_eip_origin': {
                'scope': 'source-hash', 'owner': 'owned-user',
                'prior_csv_ids': list(prior), 'duration': 60,
            },
        }
        return verified_events([r], [remote(r)], user, self.journal)

    async def prepare(self, events, user='17'):
        return await prepare_source_replay(events, user, self.journal, AsyncMock(), integration.read_csv)

    async def test_rewrite_skips_even_after_restart_and_without_old_csv(self):
        first = await self.prepare(self.events())
        self.assertEqual(await write_events(self.adapter, first, self.journal), {'created': 1, 'skipped': 0})
        self.journal = Journal()
        second = await self.prepare(self.events('完全不同的顯示名稱'))
        self.assertEqual(await write_events(self.adapter, second, self.journal), {'created': 0, 'skipped': 1})
        self.adapter.commit.assert_awaited_once()

    async def test_overlapping_automatic_and_manual_writes_create_once(self):
        automatic = await self.prepare(self.events())
        manual = await self.prepare(self.events('手動路徑重新產生的名稱'))
        entered, release = asyncio.Event(), asyncio.Event()

        async def commit(_):
            entered.set()
            await release.wait()
            return {'event_id': '123', 'detail_verified': True}

        self.adapter.commit.side_effect = commit
        running = asyncio.create_task(write_events(self.adapter, automatic, self.journal))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            with self.assertRaisesRegex(ValueError, '先前匯入結果不明'):
                await write_events(self.adapter, manual, Journal())
            self.adapter.commit.assert_awaited_once()
        finally:
            release.set()
            result = await running
        self.assertEqual(result, {'created': 1, 'skipped': 0})
        self.assertEqual(await write_events(self.adapter, manual, Journal()), {'created': 0, 'skipped': 1})
        self.adapter.commit.assert_awaited_once()

    async def test_changed_date_requires_reconciliation(self):
        await write_events(self.adapter, await self.prepare(self.events()), self.journal)
        with self.assertRaises(ValueError):
            await self.prepare(self.events(day='2026/09/11'))
        self.adapter.commit.assert_awaited_once()

    async def test_equivalent_date_padding_is_not_a_schedule_change(self):
        await write_events(self.adapter, await self.prepare(self.events(day='2026/09/10')), self.journal)
        again = await self.prepare(self.events('改名', day='2026/9/10'))
        self.assertEqual(await write_events(self.adapter, again, self.journal), {'created': 0, 'skipped': 1})
        self.adapter.commit.assert_awaited_once()

    async def test_two_concurrent_previews_cannot_commit_different_manifests(self):
        first = await self.prepare(self.events())
        second = await self.prepare(self.events(day='2026/09/11'))
        await write_events(self.adapter, first, self.journal)
        with self.assertRaises(ValueError):
            await write_events(self.adapter, second, self.journal)
        self.adapter.commit.assert_awaited_once()

    async def test_unknown_dominates_rewording(self):
        first = await self.prepare(self.events())
        self.journal.reserve_source_batch(*first[0]['source_batch'])
        self.journal.reserve_event(first[0]['key'], first[0]['aliases'])
        with self.assertRaises(ValueError):
            await write_events(self.adapter, await self.prepare(self.events('改名')), self.journal)
        self.adapter.commit.assert_not_awaited()

    async def test_different_user_does_not_share_identity(self):
        await write_events(self.adapter, await self.prepare(self.events()), self.journal)
        other = await self.prepare(self.events(user='18'), user='18')
        self.assertEqual((await write_events(self.adapter, other, self.journal))['created'], 1)

    async def test_same_time_ambiguous_tasks_not_silently_merged(self):
        events = [*self.events(), *self.events('不同工作')]
        with self.assertRaises(ValueError):
            await self.prepare(events)
        self.adapter.commit.assert_not_awaited()

    async def test_distinct_slots_preserved_order_independent(self):
        events = [*self.events(), *self.events('另一工作', day='2026/09/11')]
        await self.prepare(events)
        self.assertEqual((await write_events(self.adapter, events, self.journal))['created'], 2)
        reversed_events = [*self.events('改名二', day='2026/09/11'), *self.events('改名一')]
        await self.prepare(reversed_events)
        self.assertEqual((await write_events(self.adapter, reversed_events, self.journal))['skipped'], 2)

    async def test_legacy_receipt_binds_source_alias_without_create(self):
        old = row()
        previous = verified_events([old], [remote(old)], '17', self.journal)[0]
        self.journal.reserve_event(previous['key'], previous['aliases'])
        self.journal.done(previous['key'], aliases=previous['aliases'])
        with patch(
            'examples.official_document_workflow.application.integration.read_csv', new_callable=AsyncMock,
            return_value=([old], b'old-csv'),
        ) as read:
            events = self.events('重產的標題', prior=['old-id'])
            await prepare_source_replay(events, '17', self.journal, AsyncMock(return_value=[remote(old)]), integration.read_csv)
            read.assert_awaited_once_with('owned-user', 'old-id', default_duration_minutes=60, include_history=False)
        self.assertEqual((await write_events(self.adapter, events, self.journal))['skipped'], 1)
        # Old file may now disappear; the source alias still prevents replay.
        events = await self.prepare(self.events('第三種標題'))
        self.assertEqual((await write_events(self.adapter, events, self.journal))['skipped'], 1)
        self.adapter.commit.assert_not_awaited()

    async def test_legacy_missing_project_matches_only_unique_source_interval(self):
        old = row()
        prior = verified_events([old], [remote(old)], '17', self.journal)[0]
        self.journal.reserve_event(prior['key'], prior['aliases'])
        self.journal.done(prior['key'], aliases=prior['aliases'])
        r = self.events(prior=['legacy'])[0]['row']
        r['Description'] = '工程名稱：本次來源工程\n原文內容'
        events = verified_events([r], [remote(r)], '17', self.journal)
        with patch(
            'examples.official_document_workflow.application.integration.read_csv', new_callable=AsyncMock,
            return_value=([old], b'old'),
        ):
            await prepare_source_replay(events, '17', self.journal, AsyncMock(return_value=[remote(old)]), integration.read_csv)
        self.assertEqual((await write_events(self.adapter, events, self.journal))['skipped'], 1)
        self.adapter.commit.assert_not_awaited()

    async def test_legacy_explicit_other_project_is_not_reassigned(self):
        old = {**row(), 'Description': '工程名稱：另一工程\n來源'}
        prior = verified_events([old], [remote(old)], '17', self.journal)[0]
        self.journal.reserve_event(prior['key'], prior['aliases'])
        self.journal.done(prior['key'], aliases=prior['aliases'])
        r = self.events(prior=['legacy'])[0]['row']
        r['Description'] = '工程名稱：本次工程\n来源'
        events = verified_events([r], [remote(r)], '17', self.journal)
        with patch(
            'examples.official_document_workflow.application.integration.read_csv', new_callable=AsyncMock,
            return_value=([old], b'old'),
        ):
            with self.assertRaises(ValueError):
                await prepare_source_replay(events, '17', self.journal, AsyncMock(return_value=[remote(old)]), integration.read_csv)
        self.adapter.commit.assert_not_awaited()

    async def test_cancelled_before_write_does_not_reserve_manifest(self):
        events = await self.prepare(self.events())
        with self.assertRaises(ValueError):
            await write_events(self.adapter, events, self.journal, lambda: False)
        self.assertIsNone(self.journal.source_manifest(events[0]['source_batch'][0]))
        self.adapter.commit.assert_not_awaited()

    async def test_source_replay_check_cannot_be_omitted_by_writer(self):
        with self.assertRaises(ValueError):
            await write_events(self.adapter, self.events(), self.journal)
        self.adapter.commit.assert_not_awaited()

    async def test_origin_uses_owned_source_hash_and_only_matching_csvs(self):
        item = SimpleNamespace(id='new', meta={'eip_group_source_ids': ['src2', 'src1']})
        source = SimpleNamespace(id='src1', meta={'file_hash': 'actual-content-hash'})
        files = SimpleNamespace(get_file_by_id_and_user_id=AsyncMock(return_value=source))
        rows = [row()]
        with patch.object(integration, 'Files', files), patch.object(integration, 'prior_csv_ids', AsyncMock(return_value=['old'])) as prior:
            await annotate_csv_sources('alice', item, rows, 60)
        self.assertEqual(rows[0]['_eip_origin']['prior_csv_ids'], ['old'])
        self.assertEqual(rows[0]['_eip_origin']['owner'], 'alice')
        self.assertEqual(files.get_file_by_id_and_user_id.await_args_list[0].args, ('src1', 'alice'))
        prior.assert_awaited_once_with('alice', 'new', ['src1', 'src2'])

    async def test_missing_owned_source_blocks_origin_and_replay(self):
        files = SimpleNamespace(get_file_by_id_and_user_id=AsyncMock(return_value=None))
        item = SimpleNamespace(id='new', meta={'eip_group_source_ids': ['not-owned']})
        with patch.object(integration, 'Files', files):
            with self.assertRaises(ValueError):
                await annotate_csv_sources('alice', item, [row()], 60)
