"""Shared-calendar ACLs, event versions, durable skips and cross-owner races."""
from __future__ import annotations

import asyncio
import copy
import unittest
from datetime import date
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from examples.official_document_workflow.application import integration
from examples.official_document_workflow.application.automatic_task import AutomaticTask
from examples.official_document_workflow.domain.calendar.match_review import resolve_matches
from examples.official_document_workflow.infrastructure.eip.calendar_reconciliation import inspect_imports
from examples.official_document_workflow.infrastructure.eip.calendar_sync import write_events
from examples.official_document_workflow.infrastructure.eip.netask import js_escape
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_eip_calendar_reconciliation import EVENT
from tests.test_eip_calendar_reconciliation import fixture


def context():
    event = {
        'title': '召開115年10月份第9次安全衛生協議組織會議[AI產生](測試工程)',
        'description': '工程名稱：測試工程\n地點：工務所會議室\n115年10月份第9次安全衛生協議組織會議\n會議時間：115年10月21日。',
        'start_date': '2026-10-21', 'end_date': '2026-10-21',
        'start_time': '600', 'end_time': '660', 'all_day': '0',
        'custom_fields': ['測試第115號', '115/10/15', '公司', '', ''], 'priority': '1',
    }
    existing = copy.deepcopy(event)
    existing['description'] = existing['description'].replace('10月21日', '10月20日')
    existing['custom_fields'][:2] = ['測試第112號', '115/10/07']
    return {'incoming': event, 'candidates': [{'event_id': EVENT, 'comparison': 'conflict', 'shared': True, 'event': existing}]}


def answer(relation='duplicate'):
    return {
        'checks': [{
            'index': 0, 'candidates': [{
                'candidate_index': 0, 'relation': relation, 'reason': '工程、會議月份及次數相同，屬同一會議的通知版本。',
                'incoming_quote': '安全衛生協議組織會議', 'existing_quote': '安全衛生協議組織會議',
                'identity_evidence': [
                    {'kind': kind, 'incoming_quote': quote, 'existing_quote': quote}
                    for kind, quote in [('work', '安全衛生協議組織會議'), ('project', '測試工程'), ('instance', '115年10月份第9次')]
                ],
            }],
        }],
    }


async def resolve(value, response=None):
    matches = [{'status': 'conflict', 'review_context': value}]
    await resolve_matches([{}], matches, AsyncMock(return_value=response or answer()))
    return matches[0]


class SharedVersionTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_displayed_meeting_and_old_notice_date_is_existing(self):
        result = await resolve(context())
        self.assertEqual(result['status'], 'existing')
        self.assertTrue(result['ai_review']['shared_event'])
        self.assertFalse(result['ai_review']['schedule_revision'])

    async def test_revision_with_same_current_schedule_normalizes_to_duplicate(self):
        result = await resolve(context(), answer('revision'))
        self.assertEqual(result['status'], 'existing')
        self.assertFalse(result['ai_review']['schedule_revision'])

    async def test_actual_reschedule_does_not_create_second_event(self):
        value = context()
        old = value['candidates'][0]['event']
        old['start_date'] = old['end_date'] = '2026-10-20'
        result = await resolve(value, answer('revision'))
        self.assertEqual(result['status'], 'existing')
        self.assertTrue(result['ai_review']['schedule_revision'])

    async def test_revision_accepts_both_fifteen_day_boundaries(self):
        for offset in (-15, 15):
            value = context()
            old = value['candidates'][0]['event']
            old['start_date'] = old['end_date'] = (date(2026, 10, 21)+timedelta(days=offset)).isoformat()
            with self.subTest(offset=offset):
                result = await resolve(value, answer('revision'))
                self.assertEqual(result['status'], 'existing')
                self.assertTrue(result['ai_review']['schedule_revision'])

    async def test_model_cannot_adopt_revision_outside_fifteen_day_window(self):
        for offset in (-16, 16):
            value = context()
            old = value['candidates'][0]['event']
            old['start_date'] = old['end_date'] = (date(2026, 10, 21)+timedelta(days=offset)).isoformat()
            with self.subTest(offset=offset):
                self.assertEqual((await resolve(value, answer('revision')))['status'], 'conflict')

    async def test_all_day_milestone_and_estimate_with_different_date_labels_are_one_versioned_event(self):
        value = context()
        response = answer()
        for event, phrase in ((value['incoming'], '開工日期：115年10月15日'), (value['candidates'][0]['event'], '本工程開工日期為115年10月15日')):
            event['title'] = '工程開工[AI產生](測試工程)'
            event['description'] = '工程名稱：測試工程\n'+phrase
            event['start_date'] = event['end_date'] = '2026-10-15'
        value['candidates'][0]['event']['all_day'] = '1'
        value['incoming']['description'] += '\nAI 暫排\n待確認欄位：開始時間、結束時間'
        r = response['checks'][0]['candidates'][0]
        r['incoming_quote'] = r['existing_quote'] = '工程開工'
        r['identity_evidence'][0]['incoming_quote'] = r['identity_evidence'][0]['existing_quote'] = '工程開工'
        r['identity_evidence'][2].update(incoming_quote='開工日期：115年10月15日', existing_quote='本工程開工日期為115年10月15日')
        result = await resolve(value, response)
        self.assertEqual(result['status'], 'existing')
        self.assertTrue(result['ai_review']['schedule_revision'])
        old = value['candidates'][0]['event']
        old['description'] += '\n實際開工日期：115年10月15日'
        response['checks'][0]['candidates'][0]['identity_evidence'][2]['existing_quote'] = old['description'].split('\n', 1)[
            1
        ]
        self.assertEqual((await resolve(value, response))['status'], 'existing')

    async def test_changed_schedule_is_not_a_duplicate_without_version_evidence(self):
        value = context()
        value['candidates'][0]['event']['start_time'] = '630'
        self.assertEqual((await resolve(value))['status'], 'conflict')

    async def test_same_document_clock_conflict_is_not_assumed_to_be_a_revision(self):
        value = context()
        old = value['candidates'][0]['event']
        old['custom_fields'] = list(value['incoming']['custom_fields'])
        old['start_time'] = '630'
        self.assertEqual((await resolve(value, answer('revision')))['status'], 'conflict')

    async def test_different_month_number_project_and_executor_cannot_be_merged(self):
        for change in ('month', 'number', 'project', 'executor'):
            value = context()
            old = value['candidates'][0]['event']
            if change == 'month':
                old['title'] = old['title'].replace('10月份', '11月份')
            elif change == 'number':
                old['title'] = old['title'].replace('第9次', '第10次')
            elif change == 'project':
                old['title'] = old['title'].replace('測試工程', '另一工程')
            else:
                old['custom_fields'][3] = '監造'
                value['incoming']['custom_fields'][3] = '施工廠商'
            with self.subTest(change=change):
                self.assertEqual((await resolve(value))['status'], 'conflict')

    async def test_missing_generic_or_reused_identity_proofs_do_not_authorize_skip(self):
        for mode in ('missing', 'reused', 'different_instance'):
            response = answer()
            e = response['checks'][0]['candidates'][0]['identity_evidence']
            if mode == 'missing':
                e.pop()
            elif mode == 'reused':
                for proof in e:
                    proof['incoming_quote'] = proof['existing_quote'] = '測試工程'
            else:
                e[-1]['existing_quote'] = '工務所會議室'
            with self.subTest(mode=mode):
                self.assertEqual((await resolve(context(), response))['status'], 'conflict')

    async def test_fabricated_identity_quote_fails_entire_batch(self):
        response = answer()
        response['checks'][0]['candidates'][0]['identity_evidence'][0]['incoming_quote'] = '從未出現的工作'
        with self.assertRaises(ValueError):
            await resolve(context(), response)

    async def test_estimated_end_time_can_differ_while_real_end_time_requires_revision(self):
        value = context()
        old = value['candidates'][0]['event']
        old['end_time'] = '720'
        self.assertEqual((await resolve(value))['status'], 'conflict')
        for event in (value['incoming'], old):
            event['description'] += '\n原文未載明結束時間；AI 暫排。'
        self.assertEqual((await resolve(value))['status'], 'existing')

    async def test_shared_original_is_preferred_over_owned_copy_without_editing_either(self):
        value = context()
        own = copy.deepcopy(value['candidates'][0])
        own['shared'] = False
        own['event_id'] = '000-earlier-owned'
        value['candidates'].append(own)
        before = copy.deepcopy(value)
        response = answer()
        second = copy.deepcopy(response['checks'][0]['candidates'][0])
        second['candidate_index'] = 1
        response['checks'][0]['candidates'].append(second)
        result = await resolve(value, response)
        self.assertEqual(result['receipt']['event_id'], EVENT)
        self.assertEqual(before, value)

    async def test_adjacent_date_revision_and_shared_owner_reach_review(self):
        _, fields, listing, detail, client = fixture()
        listing[0] = detail[2] = '18'
        listing[7] = detail[9] = 'new Date(2026, 8, 9, 0, 0, 0)'
        result = await inspect_imports(client, [','.join(fields)])
        self.assertEqual(result[0]['status'], 'conflict')
        self.assertTrue(result[0]['review_context']['candidates'][0]['shared'])
        self.assertEqual(result[0]['review_context']['candidates'][0]['event']['start_date'], '2026-09-09')

    async def test_discovery_queries_event_date_plus_and_minus_fifteen_days(self):
        _, fields, listing, detail, client = fixture()
        for offset in (-15, 15):
            day = date(2026, 9, 10)+timedelta(days=offset)
            listing[0] = detail[2] = '18'
            listing[7] = detail[9] = f'new Date({day.year}, {day.month-1}, {day.day}, 0, 0, 0)'
            with self.subTest(offset=offset):
                result = await inspect_imports(client, [','.join(fields)])
                self.assertEqual(client.rpc.await_args.kwargs['start_date'], '2026-08-26')
                self.assertEqual(client.rpc.await_args.kwargs['end_date'], '2026-09-25')
                self.assertEqual(result[0]['status'], 'conflict')
                self.assertEqual(result[0]['review_context']['candidates'][0]['event']['start_date'], day.isoformat())
                self.assertTrue(result[0]['review_context']['candidates'][0]['shared'])

    async def test_out_of_range_listing_stops_before_reading_details(self):
        for offset in (-16, 16):
            _, fields, listing, _, client = fixture()
            day = date(2026, 9, 10)+timedelta(days=offset)
            listing[7] = f'new Date({day.year}, {day.month-1}, {day.day}, 0, 0, 0)'
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                await inspect_imports(client, [','.join(fields)])
            client.calendar_detail.assert_not_awaited()

    async def test_different_title_and_reference_same_day_still_reaches_ai(self):
        _, fields, listing, detail, client = fixture()
        listing[0] = detail[2] = '18'
        listing[6] = detail[8] = js_escape('現場設施查勘[AI產生](測試工程)')
        detail[37] = '新文號'
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'conflict')

    async def test_listing_acl_owner_and_date_must_match_detail(self):
        for mode in ('owner', 'date', 'missing_day', 'outside_window'):
            _, fields, listing, detail, client = fixture()
            listing[0] = '18'
            if mode != 'owner':
                detail[2] = '18'
            if mode == 'date':
                detail[9] = 'new Date(2026, 8, 9, 0, 0, 0)'
            if mode == 'missing_day':
                listing[7] = ''
            if mode == 'outside_window':
                listing[7] = 'new Date(2026, 0, 1, 0, 0, 0)'
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                await inspect_imports(client, [','.join(fields)])

    async def test_recurring_current_occurrence_uses_listed_date_not_series_start(self):
        _, fields, _listing, detail, client = fixture()
        detail[9] = 'new Date(2026, 8, 1, 0, 0, 0)'
        detail[22] = '1'
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'existing')

    async def test_other_recurring_occurrence_is_not_mistaken_for_a_reschedule(self):
        _, fields, listing, detail, client = fixture()
        listing[7] = detail[9] = 'new Date(2026, 8, 9, 0, 0, 0)'
        detail[22] = '1'
        self.assertEqual((await inspect_imports(client, [','.join(fields)]))[0]['status'], 'new')


class DurableSharedTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.journal = Journal()
        self.adapter = SimpleNamespace(
            calendar_import_matches=AsyncMock(
                return_value=[{'status': 'conflict', 'review_context': context()}],
            ), commit=AsyncMock(),
        )

    async def test_shared_skip_persists_and_never_receives_edit_receipt(self):
        event = {'key': 'shared-notice', 'line': 'validated-by-test', 'row': {}}
        self.assertEqual(await write_events(self.adapter, [event], self.journal, reviewer=AsyncMock(return_value=answer())), {'created': 0, 'skipped': 1})
        self.assertNotIn('receipt', event)
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(await write_events(self.adapter, [event], Journal()), {'created': 0, 'skipped': 1})
        with self.journal.connect() as db:
            self.assertEqual(
                db.execute(
                    'SELECT event_id FROM event_receipts WHERE key=%s',
                    ('shared-notice',),
                ).fetchone()[0], EVENT,
            )

    async def test_scheduler_uses_shared_review_and_reports_revisions_without_writes(self):
        value = context()
        value['candidates'][0]['event']['start_date'] = value['candidates'][0]['event']['end_date'] = '2026-10-20'
        self.adapter.calendar_import_matches.return_value = [{'status': 'conflict', 'review_context': value}]
        task = {'payload': {'output': {}}}
        db = SimpleNamespace(save_task=Mock())
        worker = AutomaticTask(
            db, self.journal, 'alice', task, self.adapter,
            {}, lambda: True, AsyncMock(return_value=True),
        )
        event = {'key': 'scheduler-version', 'line': 'validated', 'row': {}}
        with patch.object(integration, 'review_calendar_matches', AsyncMock(return_value=answer('revision'))):
            result = await worker.commit_calendar([event], [])
        worker.complete([event], result)
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(task['payload']['import'], {'created': 0, 'skipped': 1})
        self.assertIn('同一事件的排程版本', task['payload']['notice'])
        self.assertIn('共享行程', task['payload']['notice'])

    async def test_cross_owner_writers_wait_until_first_event_is_shared(self):
        entered, release = asyncio.Event(), asyncio.Event()
        shared = False
        commits = []

        async def matches(lines):
            return [{'status': 'existing', 'receipt': {'event_id': EVENT, 'detail_verified': True}} if shared else {'status': 'new'}]

        async def commit(line):
            commits.append(line)
            return {'event_id': EVENT, 'detail_verified': True}
        adapter = SimpleNamespace(calendar_import_matches=matches, commit=commit)

        async def first():
            nonlocal shared
            async with self.journal.calendar_lock():
                result = await write_events(adapter, [{'key': 'owner-a', 'line': 'a'}], self.journal)
                entered.set()
                await release.wait()
                shared = True
                return result
        a = asyncio.create_task(first())
        await asyncio.wait_for(entered.wait(), 2)
        b = asyncio.create_task(write_events(adapter, [{'key': 'owner-b', 'line': 'b'}], Journal()))
        try:
            await asyncio.sleep(.05)
            self.assertFalse(b.done())
            release.set()
            self.assertEqual(await a, {'created': 1, 'skipped': 0})
            self.assertEqual(await asyncio.wait_for(b, 2), {'created': 0, 'skipped': 1})
            self.assertEqual(commits, ['a'])
        finally:
            release.set()
            await asyncio.gather(a, b, return_exceptions=True)

    async def test_cancelled_lock_holder_releases_database_lock(self):
        entered = asyncio.Event()

        async def hold():
            async with self.journal.calendar_lock():
                entered.set()
                await asyncio.Event().wait()
        holder = asyncio.create_task(hold())
        await entered.wait()
        holder.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await holder

        async def reacquire():
            async with Journal().calendar_lock():
                return True
        self.assertTrue(await asyncio.wait_for(reacquire(), 2))
