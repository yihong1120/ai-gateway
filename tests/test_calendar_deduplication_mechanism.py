"""Local evidence, fresh ACL discovery and persistent no-resend integration."""
from __future__ import annotations

import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from examples.official_document_workflow.application.automatic_task import AutomaticTask
from examples.official_document_workflow.domain.calendar import deduplication as rules
from examples.official_document_workflow.domain.calendar.match_review import (
    DifferenceEvidence,
    resolve_matches,
)
from examples.official_document_workflow.infrastructure.eip.calendar_reconciliation import (
    incoming_snapshot,
    inspect_imports,
)
from examples.official_document_workflow.infrastructure.eip.calendar_sync import (
    check_existing_events,
    verified_events,
    write_events,
)
from examples.official_document_workflow.infrastructure.eip.netask import js_escape
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase
from tests.test_eip_calendar_reconciliation import EVENT, USER, fixture
from tests.test_eip_connector import remote
from tests.test_eip_shared_revisions import answer, context


def manual_pair():
    _, fields, _, _, _ = fixture()
    incoming = incoming_snapshot(fields)
    body = '主旨：測試工程設施會勘。\n說明：請辦理設施會勘，位置2K+500，附件AbCd.pdf。'
    incoming['description'] = incoming['description'].replace('\r\n', '\n').partition('\n\n公文內容：\n')[0] + '\n\n公文內容：\n' + body
    existing = copy.deepcopy(incoming)
    existing.update(title='「測試工程」設施會勘', description=body)
    return incoming, existing


def conflict(incoming, existing, **extra):
    return {'status': 'conflict', 'review_context': {'incoming': incoming, 'candidates': [
        {'event_id': EVENT, 'comparison': 'conflict', 'shared': True, 'event': existing, **extra},
    ]}}


def proof(kind, left, right):
    return DifferenceEvidence(kind=kind, incoming_quote=left, existing_quote=right)


class EvidenceTests(unittest.TestCase):
    def test_manual_same_source_work_refines_unspecified_clock(self):
        incoming, existing = manual_pair()
        incoming.update(all_day='1', start_time='', end_time='')
        self.assertTrue(rules.manual_preferred(incoming, existing))

    def test_explicit_clock_difference_adopts_manual_but_assignment_contradiction_does_not(self):
        for key, value in [('start_time', '630'), ('end_time', '720'), ('custom_fields', ['', '', '', '不同承辦人', ''])]:
            incoming, existing = manual_pair()
            if key == 'custom_fields': incoming['custom_fields'][3] = '原承辦人'
            existing[key] = value
            with self.subTest(key=key):
                self.assertEqual(rules.manual_preferred(incoming, existing), key != 'custom_fields')

    def test_manual_may_complete_an_unspecified_assignment(self):
        incoming, existing = manual_pair()
        existing['custom_fields'][3] = '原承辦人'
        self.assertTrue(rules.manual_preferred(incoming, existing))

    def test_project_date_title_alone_does_not_prove_manual_identity(self):
        incoming, existing = manual_pair()
        existing['description'] = '測試工程設施會勘'
        self.assertFalse(rules.manual_preferred(incoming, existing))

    def test_generic_work_and_other_lot_or_month_are_never_adopted(self):
        for left, right in [('事項追蹤', '事項追蹤'), ('A-1區設施會勘', 'A1區設施會勘'),
                            ('115年10月份安全會議', '115年11月份安全會議')]:
            incoming, existing = manual_pair()
            incoming['title'] = left + '[AI產生](測試工程)'
            existing['title'] = right
            with self.subTest(left=left):
                self.assertFalse(rules.manual_preferred(incoming, existing))

    def test_cjk_wrapping_preserves_latin_and_number_boundaries(self):
        self.assertEqual(rules.canonical_source('測試\n工程\r\n會勘'), '測試工程會勘')
        self.assertNotEqual(rules.canonical_source('A B 12 34'), rules.canonical_source('AB 1234'))

    def test_generated_prose_is_excluded_but_source_assignment_and_slots_remain(self):
        incoming, _ = manual_pair()
        original = rules.stable_event_facts(incoming)
        changed = copy.deepcopy(incoming)
        changed['description'] = '模型補充說明\n' + changed['description']
        changed['title'] = '辦理「測試工程」設施會勘[AI產生](測試工程)'
        self.assertEqual(original, rules.stable_event_facts(changed))
        for key, value in [('start_time', '630'), ('description', changed['description']+'新的原文'),
                           ('custom_fields', ['新文號', '', '', '', ''])]:
            other = copy.deepcopy(changed); other[key] = value
            with self.subTest(key=key):
                self.assertNotEqual(original, rules.stable_event_facts(other))

    def test_distinct_requires_source_supported_work_not_only_title_spelling(self):
        incoming, existing = manual_pair()
        existing['title'] = '材料品質檢驗'
        evidence = [proof('work', '設施會勘', '材料品質檢驗')]
        self.assertIsNone(rules.difference_proven(incoming, existing, evidence))
        existing['description'] += '並辦理材料品質檢驗。'
        self.assertEqual(rules.difference_proven(incoming, existing, evidence), 'work')

    def test_every_difference_quote_is_validated_before_accepting_any(self):
        incoming, existing = manual_pair()
        incoming['description'] = incoming['description'].replace('地點：工地東側', '地點：工地南側')
        existing['description'] = '地點：工地北側\n\n公文內容：\n' + existing['description']
        evidence = [proof('location', '地點：工地南側', '地點：工地北側'), proof('work', '虛構不存在工作', '虛構不存在工作')]
        with self.assertRaises(ValueError): rules.difference_proven(incoming, existing, evidence)

    def test_explicit_different_locations_are_positive_evidence(self):
        incoming, existing = manual_pair()
        existing['description'] = incoming['description'].replace('工地東側', '工地西側')
        self.assertEqual(rules.difference_proven(incoming, existing, [proof('location', '地點：工地東側', '地點：工地西側')]), 'location')

    def test_generic_work_cannot_be_authorized_by_different_party_alone(self):
        incoming, existing = manual_pair()
        incoming['title'] = '本案事項[AI產生](測試工程)'
        existing['title'] = '測試工程後續處理'
        for event, party in ((incoming, '甲方承辦人'), (existing, '乙方承辦人')):
            event['custom_fields'][3] = party
            event['description'] += '\n'+party
        self.assertIsNone(rules.difference_proven(incoming, existing, [proof('party', '甲方承辦人', '乙方承辦人')]))

    def test_original_date_is_bounded_and_requires_actual_reschedule(self):
        incoming, _ = manual_pair()
        incoming['description'] += '\n原訂115年7月1日，延期至115年9月10日。'
        self.assertEqual(rules.original_schedule_days(incoming), ('2026-07-01',))
        incoming['description'] = incoming['description'].replace('延期', '通知')
        self.assertEqual(rules.original_schedule_days(incoming), ())

    def test_invalid_incomplete_distant_and_excessive_original_dates_stop(self):
        examples = ['原訂115年2月30日延期', '原訂7月1日延期至115年9月10日', '原訂113年7月1日延期',
                    '原訂115年7月1日；原定115年8月1日；原安排115年8月2日。延期']
        for text in examples:
            incoming, _ = manual_pair(); incoming['description'] += '\n'+text
            with self.subTest(text=text), self.assertRaises(ValueError): rules.original_schedule_days(incoming)

    def test_generated_reschedule_claim_without_source_does_not_widen_lookup(self):
        incoming, _ = manual_pair()
        incoming['description'] = '原訂115年7月1日延期\n' + incoming['description']
        self.assertEqual(rules.original_schedule_days(incoming), ())


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_manual_times_or_assignees_stop_without_model(self):
        for key, value in [('start_time', '660'), ('custom_fields', ['', '', '', '另一承辦人', ''])]:
            incoming, existing = manual_pair(); incoming['all_day'] = '1'
            existing['custom_fields'][3] = '原承辦人'
            matches = [conflict(incoming, existing)]
            other = copy.deepcopy(matches[0]['review_context']['candidates'][0])
            other['event_id'] = 'other-manual'; other['event'][key] = value
            matches[0]['review_context']['candidates'].append(other)
            reviewer = AsyncMock()
            await resolve_matches([{}], matches, reviewer)
            with self.subTest(key=key):
                self.assertEqual(matches[0]['status'], 'conflict')
                reviewer.assert_not_awaited()

    async def test_manual_adoption_notice_does_not_claim_a_model_call(self):
        task = SimpleNamespace(task={}, db=SimpleNamespace(save_task=Mock()), payload={
            'import': {'created': 0, 'skipped': 1}, 'output': {}, 'calendar_reconciliation': [
                {'method': 'source_evidence', 'decision': 'existing', 'manual_preferred': True}]})
        AutomaticTask.complete(task, [{}], {'attached_events': 0})
        self.assertIn('未呼叫行程核對模型', task.payload['notice'])
        self.assertNotIn('AI 已自動核對', task.payload['notice'])
        task.payload['calendar_reconciliation'].append({'method': 'ai', 'decision': 'existing'})
        AutomaticTask.complete(task, [{}], {'attached_events': 0})
        self.assertIn('AI 已自動核對 1 筆', task.payload['notice'])
        self.assertNotIn('AI 已自動核對 2 筆', task.payload['notice'])

    async def test_manual_adoption_needs_no_model_even_when_optional_reviewer_absent(self):
        incoming, existing = manual_pair(); matches = [conflict(incoming, existing)]
        await resolve_matches([{}], matches, None)
        self.assertEqual(matches[0]['status'], 'existing')
        self.assertTrue(matches[0]['ai_review']['manual_preferred'])

    async def test_unknown_clock_prefers_visible_manual_without_model(self):
        incoming, existing = manual_pair(); incoming['all_day'] = '1'
        reviewer = AsyncMock(); matches = [conflict(incoming, existing)]
        await resolve_matches([{}], matches, reviewer)
        reviewer.assert_not_awaited()
        self.assertEqual(matches[0]['receipt']['event_id'], EVENT)

    async def test_manual_priority_precedes_owned_ai_copy_and_does_not_modify_any(self):
        incoming, existing = manual_pair(); matches = [conflict(incoming, existing)]
        ai = {'event_id': 'earlier-ai', 'comparison': 'same', 'shared': False, 'event': copy.deepcopy(incoming)}
        matches[0]['review_context']['candidates'].append(ai)
        original = copy.deepcopy(existing)
        await resolve_matches([{}], matches, AsyncMock())
        self.assertEqual(matches[0]['receipt']['event_id'], EVENT)
        self.assertEqual(existing, original)

    async def test_without_reviewer_uncertain_manual_does_not_authorize_new(self):
        incoming, existing = manual_pair()
        incoming['custom_fields'][3], existing['custom_fields'][3] = '甲單位', '乙單位'
        matches = [conflict(incoming, existing)]
        await resolve_matches([{}], matches, None)
        self.assertEqual(matches[0]['status'], 'conflict')

    async def test_documented_long_reschedule_requires_three_identity_proofs(self):
        value = context(); old = value['candidates'][0]['event']
        old['start_date'] = old['end_date'] = '2026-07-01'
        value['incoming']['description'] += '\n原訂115年7月1日，延期至115年10月21日。'
        matches = [{'status': 'conflict', 'review_context': value}]
        await resolve_matches([{}], matches, AsyncMock(return_value=answer('revision')))
        self.assertEqual(matches[0]['status'], 'existing')
        self.assertTrue(matches[0]['ai_review']['schedule_revision'])
        response = answer('revision'); response['checks'][0]['candidates'][0]['identity_evidence'] = []
        matches = [{'status': 'conflict', 'review_context': value}]
        await resolve_matches([{}], matches, AsyncMock(return_value=response))
        self.assertEqual(matches[0]['status'], 'conflict')

    async def test_lookup_reads_exact_original_day_beyond_normal_window(self):
        source, fields, listing, detail, client = fixture()
        fields[19] = js_escape(source['Description']+'\n原訂115年7月1日，延期至115年9月10日。')
        listing[7] = detail[9] = 'new Date(2026, 6, 1, 0, 0, 0)'
        # A different title must still be examined at the explicitly quoted old date.
        listing[6] = detail[8] = js_escape('現場查驗工作')
        client.rpc.side_effect = [[[0, '']], [[0, ''], listing]]
        result = await inspect_imports(client, [','.join(fields)])
        self.assertEqual(result[0]['status'], 'conflict')
        self.assertEqual(client.rpc.await_count, 2)
        self.assertEqual(client.rpc.await_args.kwargs['start_date'], '2026-07-01')
        self.assertEqual(client.rpc.await_args.kwargs['end_date'], '2026-07-01')
        client.calendar_detail.assert_awaited_once()

    async def test_old_date_query_failure_and_rows_outside_query_stop(self):
        for mode in ('failure', 'outside', 'overflow'):
            source, fields, listing, _, client = fixture()
            fields[19] = js_escape(source['Description']+'\n原訂115年7月1日延期。')
            second = TimeoutError('offline') if mode == 'failure' else [[0, ''], listing]
            if mode == 'overflow': second = [[0, '']]+[listing]*501
            client.rpc.side_effect = [[[0, '']], second]
            with self.subTest(mode=mode), self.assertRaises((ValueError, TimeoutError)):
                await inspect_imports(client, [','.join(fields)])

    async def test_distinct_without_positive_proof_is_blocked_even_different_document(self):
        value = context(); response = answer('distinct')
        # A different notice date does not prove a different duty.
        response['checks'][0]['candidates'][0].update(incoming_quote='10月21日', existing_quote='10月20日')
        matches = [{'status': 'conflict', 'review_context': value}]
        await resolve_matches([{}], matches, AsyncMock(return_value=response))
        self.assertEqual(matches[0]['status'], 'conflict')

    async def test_same_work_with_proven_different_assignees_can_be_added(self):
        value = context(); left, right = value['incoming'], value['candidates'][0]['event']
        left['custom_fields'][3], right['custom_fields'][3] = '甲方承辦人', '乙方承辦人'
        left['description'] += '\n辦理人／單位：甲方承辦人'
        right['description'] += '\n辦理人／單位：乙方承辦人'
        response = answer('distinct')
        response['checks'][0]['candidates'][0]['difference_evidence'] = [
            {'kind': 'party', 'incoming_quote': '辦理人／單位：甲方承辦人', 'existing_quote': '辦理人／單位：乙方承辦人'}]
        matches = [{'status': 'conflict', 'review_context': value}]
        await resolve_matches([{}], matches, AsyncMock(return_value=response))
        self.assertEqual(matches[0]['status'], 'new')


class PersistentDeliveryTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source, _, _, _, _ = fixture()
        self.journal = Journal()
        self.adapter = SimpleNamespace(calendar_import_matches=AsyncMock(side_effect=lambda lines: [{'status': 'new'} for _ in lines]),
                                       commit=AsyncMock(return_value={'event_id': EVENT, 'detail_verified': True}))

    def events(self, source=None, user=USER, journal=None):
        source = source or self.source
        return verified_events([source], [remote(source)], user, journal or self.journal)

    def regenerated(self):
        source = copy.deepcopy(self.source)
        source['Subject'] = '辦理「測試工程」設施會勘[AI產生](測試工程)'
        source['Description'] = '判讀補充：請依原文辦理。\n' + source['Description']
        return source

    async def test_regenerated_csv_and_new_journal_skip_without_remote_or_model(self):
        await write_events(self.adapter, self.events(), self.journal)
        restarted = Journal(); events = self.events(self.regenerated(), journal=restarted)
        self.adapter.calendar_import_matches.reset_mock(); reviewer = AsyncMock()
        self.assertEqual(await write_events(self.adapter, events, restarted, reviewer=reviewer), {'created': 0, 'skipped': 1})
        self.adapter.calendar_import_matches.assert_not_awaited(); reviewer.assert_not_awaited()
        self.adapter.commit.assert_awaited_once()

    async def test_content_alias_unknown_stops_stale_preview_before_remote(self):
        stale = self.events(self.regenerated())
        current = self.events(); self.journal.reserve_event(current[0]['key'], current[0]['aliases'])
        with self.assertRaises(ValueError): await write_events(self.adapter, stale, self.journal, reviewer=AsyncMock())
        self.adapter.calendar_import_matches.assert_not_awaited(); self.adapter.commit.assert_not_awaited()

    async def test_unknown_alias_dominates_done_alias_after_preview(self):
        stale = self.events(); aliases = stale[0]['aliases']
        self.journal.reserve_event(stale[0]['key'])
        self.journal.done(stale[0]['key'])
        self.journal.reserve_event(aliases[-1])
        with self.assertRaises(ValueError):
            await check_existing_events(stale, self.journal, self.adapter.calendar_import_matches, block_conflicts=True)
        self.adapter.calendar_import_matches.assert_not_awaited()

    async def test_private_recipient_scope_does_not_suppress_other_accounts(self):
        await write_events(self.adapter, self.events(), self.journal)
        other = self.events(user='18')
        self.assertEqual(await write_events(self.adapter, other, self.journal), {'created': 1, 'skipped': 0})
        self.assertEqual(self.adapter.commit.await_count, 2)

    async def test_ten_accounts_adopt_only_after_shared_event_is_visible(self):
        visible = False
        async def read(lines):
            return [{'status': 'existing', 'receipt': {'event_id': EVENT, 'detail_verified': True}}
                    if visible else {'status': 'new'} for _ in lines]
        self.adapter.calendar_import_matches.side_effect = read
        async def worker(number):
            nonlocal visible
            journal = Journal()
            async with journal.calendar_lock():
                # As in AutomaticTask, retain the lock through project sharing.
                result = await write_events(self.adapter, self.events(user=str(number), journal=journal), journal)
                visible = True
                return result
        results = await asyncio.gather(*(worker(n) for n in range(17, 27)))
        self.assertEqual(sum(r['created'] for r in results), 1)
        self.assertEqual(sum(r['skipped'] for r in results), 9)
        self.adapter.commit.assert_awaited_once()

    async def test_ten_private_calendars_receive_one_each_and_no_extra_on_replay(self):
        results = await asyncio.gather(*(write_events(self.adapter, self.events(user=str(n)), Journal()) for n in range(17, 27)))
        self.assertEqual(sum(r['created'] for r in results), 10)
        replay = await asyncio.gather(*(write_events(self.adapter, self.events(self.regenerated(), user=str(n)), Journal()) for n in range(17, 27)))
        self.assertEqual(sum(r['skipped'] for r in replay), 10)
        self.assertEqual(self.adapter.commit.await_count, 10)

    async def test_two_workers_regenerated_content_commit_once(self):
        left, right = self.events(), self.events(self.regenerated())
        results = await asyncio.gather(write_events(self.adapter, left, self.journal), write_events(self.adapter, right, Journal()))
        self.assertEqual(sum(r['created'] for r in results), 1)
        self.adapter.commit.assert_awaited_once()

    async def test_unknown_network_outcome_never_retries_regenerated_source(self):
        self.adapter.commit.side_effect = TimeoutError('response lost')
        with self.assertRaises(TimeoutError): await write_events(self.adapter, self.events(), self.journal)
        self.adapter.commit.reset_mock()
        with self.assertRaises(ValueError): await write_events(self.adapter, self.events(self.regenerated()), Journal())
        self.adapter.commit.assert_not_awaited()

    async def test_absent_remote_after_done_does_not_resurrect_deleted_event(self):
        await write_events(self.adapter, self.events(), self.journal)
        self.adapter.calendar_import_matches.reset_mock()
        self.assertEqual(await write_events(self.adapter, self.events(self.regenerated()), Journal()), {'created': 0, 'skipped': 1})
        self.adapter.calendar_import_matches.assert_not_awaited()

    async def test_visible_manual_adoption_persists_receipt_without_optional_model(self):
        incoming, existing = manual_pair()
        source = copy.deepcopy(self.source); source['Description'] = incoming['description']
        self.adapter.calendar_import_matches.side_effect = None
        self.adapter.calendar_import_matches.return_value = [conflict(incoming, existing)]
        events = self.events(source)
        self.assertEqual(await write_events(self.adapter, events, self.journal), {'created': 0, 'skipped': 1})
        self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.journal.status(events[0]['key']), 'done')
        self.assertEqual(events[0]['existing_receipt']['event_id'], EVENT)

    async def test_changed_assignment_and_actual_time_stay_separate(self):
        await write_events(self.adapter, self.events(), self.journal)
        source = self.regenerated(); source['Custom Field4'] = '乙單位'
        self.assertEqual(await write_events(self.adapter, self.events(source), self.journal), {'created': 1, 'skipped': 0})
        source['Start Time'] = '上午10:30:00'; source['Start Time(24hr)'] = '10:30'
        self.assertEqual(await write_events(self.adapter, self.events(source), self.journal), {'created': 1, 'skipped': 0})

    async def test_model_failure_does_not_reserve_or_authorize_next_write(self):
        incoming, existing = manual_pair()
        incoming['custom_fields'][3], existing['custom_fields'][3] = '甲單位', '乙單位'
        self.adapter.calendar_import_matches.side_effect = None
        self.adapter.calendar_import_matches.return_value = [conflict(incoming, existing)]
        events = self.events()
        with self.assertRaises(TimeoutError):
            await write_events(self.adapter, events, self.journal, reviewer=AsyncMock(side_effect=TimeoutError('model offline')))
        self.assertIsNone(self.journal.status(events[0]['key']))
        self.adapter.commit.assert_not_awaited()
