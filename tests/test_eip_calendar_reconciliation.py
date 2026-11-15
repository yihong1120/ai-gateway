"""Read-only cross-entry matching and safe receipt adoption, no live EIP."""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import examples.official_document_workflow.infrastructure.eip.calendar_csv as _calendar_csv_imports
from .test_eip_connector import remote
from .test_eip_connector import row
from examples.official_document_workflow.infrastructure.eip.calendar_reconciliation import (
    inspect_imports,
)
from examples.official_document_workflow.infrastructure.eip.calendar_sync import check_existing_events
from examples.official_document_workflow.infrastructure.eip.calendar_sync import verified_events
from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events
from examples.official_document_workflow.infrastructure.eip.netask import import_line
from examples.official_document_workflow.infrastructure.eip.netask import js_escape
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase

USER = '17'
EVENT = '2026-09-10 10:00:00.001;1001'


def fixture():
    source = {
        **row(), 'Subject': '設施會勘[AI產生](測試工程)',
        'Start Time': '上午10:00:00', 'Start Time(24hr)': '10:00',
        'End Time': '上午11:00:00', 'End Time(24hr)': '11:00',
        'All day event': '假', 'Private': '真',
        'Custom Field1': '測試字第115001號',
        'Description': '辦理事項：設施會勘\n工程名稱：測試工程\n地點：工地東側\n\n公文內容：\n請依附件AbCd.pdf辦理設施會勘，位置2K+500。',
    }
    fields = import_line(remote(source), USER).split(',')
    listing = [''] * 21
    for index, value in {0: USER, 2: EVENT, 6: fields[0], 7: 'new Date(2026, 8, 10, 0, 0, 0)', 8: fields[5], 9: fields[2], 10: fields[4]}.items():
        listing[index] = value
    detail = [''] * 43
    for index, value in {
        0: EVENT, 2: USER, 4: '', 8: fields[0], 9: 'new Date(2026, 8, 10, 0, 0, 0)',
        10: fields[5], 11: fields[2], 12: fields[4], 13: fields[20], 14: fields[19], 15: 'Ru17',
    }.items():
        detail[index] = value
    detail[37:42] = fields[23:28]
    client = SimpleNamespace(
        user_id=USER, rpc=AsyncMock(
            return_value=[[0, ''], listing],
        ), calendar_detail=AsyncMock(return_value=[detail, [0]]),
    )
    return source, fields, listing, detail, client


class ExistingEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_sibling_all_day_segments_are_not_mistaken_for_schedule_revisions(self):

        source, _, listing, detail, client = fixture()
        source.update({
            'All day event': '真', 'Start Time': '', 'Start Time(24hr)': '',
            'End Time': '', 'End Time(24hr)': '', 'End Date': '2026/09/12',
        })
        segments = _calendar_csv_imports.import_segments([source])
        first = import_line(remote(segments[0]), USER).split(',')
        second = import_line(remote(segments[1]), USER).split(',')
        listing[6], listing[8], listing[9], listing[10] = first[0], first[5], first[2], first[4]
        detail[8], detail[10], detail[11], detail[12], detail[14] = first[0], first[5], first[2], first[4], first[19]
        detail[37:42] = first[23:28]
        self.assertEqual((await inspect_imports(client, [','.join(second)]))[0]['status'], 'new')
        detail[37] = '不同文號'
        self.assertEqual((await inspect_imports(client, [','.join(second)]))[0]['status'], 'conflict')

    async def test_native_event_is_identified_without_writing(self):
        _, fields, _, _, client = fixture()
        result = await inspect_imports(client, [','.join(fields)])
        self.assertEqual(result, [{'status': 'existing', 'receipt': {'event_id': EVENT, 'detail_verified': True}}])
        self.assertEqual(client.rpc.await_args.args, ('List',))

    async def test_empty_calendar_is_new_without_detail_requests(self):
        _, fields, _, _, client = fixture()
        client.rpc.return_value = [[0, '']]
        self.assertEqual(await inspect_imports(client, [','.join(fields)]), [{'status': 'new'}])
        client.calendar_detail.assert_not_awaited()

    async def test_prefix_and_known_native_ascii_uppercase_keep_source_identity(self):
        source, fields, listing, detail, client = fixture()
        listing[6] = detail[8] = js_escape('辦理' + source['Subject'])
        detail[14] = js_escape(source['Description'].replace('辦理事項：設施會勘', '辦理事項：辦理設施會勘').upper())
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'existing')

    async def test_quoted_generated_project_prefix_matches_without_model_or_write(self):
        _, fields, listing, detail, client = fixture()
        fields[0] = js_escape('「測試工程」設施會勘[AI產生](測試工程)')
        listing[6] = detail[8] = js_escape('辦理測試工程設施會勘[AI產生](測試工程)')
        self.assertEqual(
            (await inspect_imports(client, [','.join(fields)]))[0],
            {'status': 'existing', 'receipt': {'event_id': EVENT, 'detail_verified': True}},
        )

    async def test_project_quote_alias_does_not_remove_work_or_lot_punctuation(self):
        _, fields, listing, detail, client = fixture()
        fields[0] = js_escape('「測試工程」A-1區設施會勘[AI產生](測試工程)')
        listing[6] = detail[8] = js_escape('辦理測試工程A1區設施會勘[AI產生](測試工程)')
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'conflict')

    async def test_user_project_and_sharing_settings_do_not_trigger_an_edit(self):
        _, fields, _, detail, client = fixture()
        detail[4], detail[15] = '77', 'Ru17,Ru18'
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'existing')
        self.assertEqual(detail[4:5], ['77'])
        self.assertTrue(all(call.args == ('List',) for call in client.rpc.await_args_list))

    async def test_another_document_is_sent_for_semantic_review(self):
        _, fields, _, detail, client = fixture()
        detail[37] = '另一份公文字第115002號'
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'conflict')

    async def test_changes_to_source_schedule_executor_or_priority_require_review(self):
        for index, value in ((11, '630'), (12, '690'), (13, '2'), (14, js_escape('原文已經變更')), (40, '其他辦理人')):
            _, fields, _, detail, client = fixture()
            detail[index] = value
            with self.subTest(index=index):
                self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'conflict')

    async def test_two_existing_copies_are_not_silently_adopted(self):
        _, fields, listing, detail, client = fixture()
        second_id = '2026-09-10 10:00:00.002;1002'
        second_list, second_detail = list(listing), list(detail)
        second_list[2] = second_detail[0] = second_id
        client.rpc.return_value.append(second_list)
        client.calendar_detail.side_effect = [[detail, [0]], [second_detail, [0]]]
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'conflict')

    async def test_visible_shared_event_is_adopted_without_an_edit(self):
        _, fields, listing, detail, client = fixture()
        listing[0] = detail[2] = '18'
        self.assertEqual(await inspect_imports(client, [','.join(fields)]), [{'status': 'existing', 'receipt': {'event_id': EVENT, 'detail_verified': True}}])
        client.calendar_detail.assert_awaited_once()

    async def test_changed_detail_owner_or_invalid_listing_stops_before_write(self):
        _, fields, _, detail, client = fixture()
        detail[2] = '18'
        with self.assertRaisesRegex(ValueError, '擁有人'):
            await inspect_imports(client, [','.join(fields)])
        client.rpc.return_value = [[0, ''], ['incomplete']]
        with self.assertRaisesRegex(ValueError, '清單'):
            await inspect_imports(client, [','.join(fields)])

    async def test_exact_duplicate_candidates_fail_before_any_remote_call(self):
        _, fields, _, _, client = fixture()
        with self.assertRaisesRegex(ValueError, '重複行程'):
            await inspect_imports(client, [','.join(fields)] * 2)
        client.rpc.assert_not_awaited()

    async def test_batch_reuses_daily_listing_and_detail_cache(self):
        _, fields, _, _, client = fixture()
        other = list(fields)
        other[0], other[23] = js_escape('另一工作[AI產生]'), '另一文號'
        result = await inspect_imports(client, [','.join(fields), ','.join(other)])
        self.assertEqual([item['status'] for item in result], ['existing', 'conflict'])
        client.rpc.assert_awaited_once()
        client.calendar_detail.assert_awaited_once()


class ReceiptAdoptionTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        source, _, _, _, self.client = fixture()
        self.journal = Journal()
        self.events = verified_events([source], [remote(source)], USER, self.journal)

        async def matches(lines):
            return await inspect_imports(self.client, lines)

        self.adapter = SimpleNamespace(
            calendar_import_matches=AsyncMock(side_effect=matches),
            commit=AsyncMock(),
        )

    async def test_preview_is_read_only_and_confirmation_adopts_durable_receipt(self):
        before = copy.deepcopy(self.events)
        await check_existing_events(self.events, self.journal, self.adapter.calendar_import_matches)
        self.assertIsNone(self.journal.status(self.events[0]['key']))
        result = await write_events(self.adapter, self.events, self.journal)
        self.assertEqual(result, {'created': 0, 'skipped': 1})
        self.adapter.commit.assert_not_awaited()
        self.assertNotIn('receipt', self.events[0])  # No project/attachment writes.
        self.assertEqual(self.events[0]['row'], before[0]['row'])
        with self.journal.connect() as db:
            self.assertEqual(
                db.execute(
                    'SELECT event_id FROM event_receipts WHERE key=%s',
                    (self.events[0]['key'],),
                ).fetchone()[0], EVENT,
            )
        self.adapter.calendar_import_matches.reset_mock()
        self.assertEqual(await write_events(self.adapter, self.events, Journal()), {'created': 0, 'skipped': 1})
        self.adapter.calendar_import_matches.assert_not_awaited()

    async def test_read_failure_leaves_no_unknown_write_and_no_commit(self):
        self.adapter.calendar_import_matches.side_effect = TimeoutError('read failed')
        with self.assertRaises(TimeoutError):
            await write_events(self.adapter, self.events, self.journal)
        self.assertIsNone(self.journal.status(self.events[0]['key']))
        self.adapter.commit.assert_not_awaited()

    async def test_conflict_or_incomplete_response_blocks_the_entire_batch(self):
        for response in ([{'status': 'conflict', 'reason': '內容已修改'}], [], [{'status': 'existing'}]):
            self.adapter.calendar_import_matches.side_effect = None
            self.adapter.calendar_import_matches.return_value = response
            with self.subTest(response=response), self.assertRaises(ValueError):
                await write_events(self.adapter, self.events, self.journal)
            self.assertIsNone(self.journal.status(self.events[0]['key']))
            self.adapter.commit.assert_not_awaited()

    async def test_unknown_prior_write_is_not_overridden_by_an_existing_event(self):
        self.journal.reserve_event(self.events[0]['key'], self.events[0]['aliases'])
        with self.assertRaisesRegex(ValueError, '結果不明'):
            await write_events(self.adapter, self.events, self.journal)
        self.adapter.calendar_import_matches.assert_not_awaited()
        self.assertEqual(self.journal.status(self.events[0]['key']), 'unknown')
