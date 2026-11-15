"""Semantic duplicate decisions, fresh snapshots, and guarded write integration."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from examples.official_document_workflow.domain.calendar.match_review import (
    REVISION_DAYS,
    resolve_matches,
    same_source_work,
)
from examples.official_document_workflow.infrastructure.eip.calendar_reconciliation import (
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

from .test_eip_calendar_reconciliation import EVENT, USER, fixture
from .test_eip_connector import remote


def response(relation='duplicate', incoming='設施會勘', existing='設施會勘'):
    return {'checks': [{'index': 0, 'candidates': [{
        'candidate_index': 0, 'relation': relation, 'reason': '依雙方原文核對具體工作與排程。',
        'incoming_quote': incoming, 'existing_quote': existing,
        'difference_evidence': ([{'kind': 'work', 'incoming_quote': incoming, 'existing_quote': existing}]
                                if relation == 'distinct' else []),
    }]}]}


async def ambiguous():
    source, fields, listing, detail, client = fixture()
    detail[8] = listing[6] = js_escape('纜線設施現場勘查[AI產生](測試工程)')
    matches = await inspect_imports(client, [','.join(fields)])
    return source, fields, detail, client, matches


async def reported_aliases():
    source, fields, listing, detail, client = fixture()
    fields[0] = js_escape('「測試工程」設施會勘[AI產生](測試工程)')
    fields[26] = '本分局'
    source['Subject'] = '「測試工程」設施會勘[AI產生](測試工程)'
    source['Custom Field4'] = '本分局'
    source['Description'] = source['Description'].replace('\n\n公文內容：', '\n辦理人／單位：本分局\n\n公文內容：')
    fields[19] = js_escape(source['Description'])
    listing[6] = detail[8] = js_escape('辦理測試工程設施會勘[AI產生](測試工程)')
    # The shared copy lacks the executor field; its full quoted source is identical.
    detail[40] = ''
    matches = await inspect_imports(client, [','.join(fields)])
    answer = response('distinct', incoming='「測試工程」設施會勘', existing='辦理測試工程設施會勘')
    return source, fields, detail, client, matches, answer


class SemanticReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_source_project_quote_and_missing_executor_cannot_authorize_distinct(self):
        _, _, _, _, matches, answer = await reported_aliases()
        context = matches[0]['review_context']
        self.assertTrue(same_source_work(context['incoming'], context['candidates'][0]['event']))
        await resolve_matches([{}], matches, AsyncMock(return_value=answer))
        self.assertEqual(matches[0]['status'], 'conflict')

    async def test_new_guard_does_not_reuse_older_distinct_review_cache(self):
        _, _, _, _, matches, answer = await reported_aliases()
        context = matches[0]['review_context']
        old = hashlib.sha256(json.dumps(['shared-revisions-v1', REVISION_DAYS, context], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        events = [{'_ai_match_cache': {'fingerprint': old, 'result': {'status': 'new'}}}]
        reviewer = AsyncMock(return_value=answer)
        await resolve_matches(events, matches, reviewer)
        reviewer.assert_awaited_once()
        self.assertEqual(matches[0]['status'], 'conflict')

    async def test_same_source_veto_keeps_different_slot_party_source_and_lot_distinctions(self):
        _, _, _, _, matches, _ = await reported_aliases()
        context = matches[0]['review_context']; incoming = context['incoming']; existing = context['candidates'][0]['event']
        for key, value in (
            ('title', '測試工程A1區設施會勘[AI產生](測試工程)'),
            ('start_date', '2026-09-11'), ('start_time', '720'),
            ('description', existing['description'] + '\n不同具體工作'),
            ('custom_fields', ['另一文號', *existing['custom_fields'][1:]]),
        ):
            with self.subTest(key=key):
                self.assertFalse(same_source_work(incoming, {**existing, key: value}))
        other = copy.deepcopy(existing)
        other['custom_fields'][3] = '另一分局'
        self.assertFalse(same_source_work(incoming, other))

    async def test_wording_difference_automatically_adopts_existing_and_caches_snapshot(self):
        _, _, _, _, matches = await ambiguous()
        original = copy.deepcopy(matches)
        events = [{}]; reviewer = AsyncMock(return_value=response())
        await resolve_matches(events, matches, reviewer)
        self.assertEqual(matches[0]['status'], 'existing')
        self.assertEqual(matches[0]['receipt']['event_id'], EVENT)
        self.assertEqual(matches[0]['ai_review']['decision'], 'existing')
        request = reviewer.await_args.args[0]
        self.assertNotIn('event_id', str(request))
        again = copy.deepcopy(original)
        await resolve_matches(events, again, reviewer)
        reviewer.assert_awaited_once()
        self.assertEqual(again, matches)
        changed = copy.deepcopy(original)
        changed[0]['review_context']['candidates'][0]['event']['description'] += '\n更新紀錄'
        await resolve_matches(events, changed, reviewer)
        self.assertEqual(reviewer.await_count, 2)

    async def test_distinct_work_at_same_time_can_be_added(self):
        _, _, _, _, matches = await ambiguous()
        matches[0]['review_context']['candidates'][0]['event']['title'] = '材料品質檢驗'
        matches[0]['review_context']['candidates'][0]['event']['description'] += '\n材料品質檢驗'
        reviewer = AsyncMock(return_value=response('distinct', existing='材料品質檢驗'))
        await resolve_matches([{}], matches, reviewer)
        self.assertEqual(matches[0]['status'], 'new')

    async def test_schedule_and_executor_conflicts_cannot_be_overruled_as_duplicates(self):
        for field, value in [('start_time', '630'), ('custom_fields', ['文號已變', '', '', '', ''])]:
            _, _, _, _, matches = await ambiguous()
            matches[0]['review_context']['candidates'][0]['event'][field] = value
            await resolve_matches([{}], matches, AsyncMock(return_value=response()))
            self.assertEqual(matches[0]['status'], 'conflict')

    async def test_uncertainty_or_identical_distinct_evidence_stays_blocked(self):
        for relation in ('uncertain', 'distinct'):
            _, _, _, _, matches = await ambiguous()
            await resolve_matches([{}], matches, AsyncMock(return_value=response(relation)))
            self.assertEqual(matches[0]['status'], 'conflict')

    async def test_multiple_verified_copies_are_skipped_without_deleting_any(self):
        _, _, _, _, matches = await ambiguous()
        context = matches[0]['review_context']; other = copy.deepcopy(context['candidates'][0]);other['event_id'] = 'later-copy';context['candidates'].append(other)
        answer = response();second = copy.deepcopy(answer['checks'][0]['candidates'][0]);second['candidate_index'] = 1;answer['checks'][0]['candidates'].append(second)
        await resolve_matches([{}], matches, AsyncMock(return_value=answer))
        self.assertEqual(matches[0]['status'], 'existing')
        self.assertEqual(matches[0]['ai_review']['existing_matches'], 2)

    async def test_exact_match_cannot_be_changed_to_new_by_model(self):
        _, _, _, _, matches = await ambiguous()
        matches[0]['review_context']['candidates'][0]['comparison'] = 'same'
        await resolve_matches([{}], matches, AsyncMock(return_value=response('distinct')))
        self.assertEqual(matches[0]['status'], 'existing')

    async def test_invalid_quotes_indices_missing_items_and_instructions_do_not_authorize(self):
        invalid = [response(existing='編造引用的工作'), {'checks': []}]
        for key, value in [('candidate_index', True), ('candidate_index', 99), ('relation', 'delete'), ('reason', '')]:
            answer=response();answer['checks'][0]['candidates'][0][key]=value;invalid.append(answer)
        answer=response();answer['checks']*=2;invalid.append(answer)
        for answer in invalid:
            _, _, _, _, matches = await ambiguous();events=[{}]
            with self.subTest(answer=answer), self.assertRaises(ValueError):
                await resolve_matches(events, matches, AsyncMock(return_value=answer))
            self.assertNotIn('_ai_match_cache', events[0])

    async def test_batch_validates_before_applying_and_uses_one_call(self):
        _, _, _, _, matches = await ambiguous();matches *= 2;events=[{},{}]
        answer=response();other=copy.deepcopy(answer['checks'][0]);other['index']=1;other['candidates'][0]['existing_quote']='無法找到的原文';answer['checks'].append(other)
        with self.assertRaises(ValueError):await resolve_matches(events, matches, AsyncMock(return_value=answer))
        self.assertTrue(all('_ai_match_cache' not in e for e in events))
        answer['checks'][1]['candidates'][0]['existing_quote']='設施會勘';reviewer=AsyncMock(return_value=answer)
        await resolve_matches(events, matches, reviewer)
        reviewer.assert_awaited_once();self.assertEqual(len(reviewer.await_args.args[0]['events']),2)

    async def test_exact_or_empty_calendar_does_not_spend_model_call(self):
        _, fields, _, _, client=fixture();matches=await inspect_imports(client,[','.join(fields)])
        reviewer=AsyncMock();await resolve_matches([{}],matches,reviewer);reviewer.assert_not_awaited()

    async def test_larger_batch_is_bounded_without_losing_global_indices(self):
        _, _, _, _, original = await ambiguous()
        matches = [copy.deepcopy(original[0]) for _ in range(5)]
        async def reply(request):
            checks = []
            for item in request['events']:
                check = response()['checks'][0]
                check['index'] = item['index']
                checks.append(check)
            return {'checks': checks}
        reviewer = AsyncMock(side_effect=reply)
        await resolve_matches([{} for _ in matches], matches, reviewer)
        self.assertEqual(reviewer.await_count, 2)
        self.assertEqual([len(c.args[0]['events']) for c in reviewer.await_args_list], [4, 1])
        self.assertTrue(all(m['status'] == 'existing' for m in matches))

    async def test_limits_are_enforced_before_model_call_without_truncation(self):
        for too_many in (True, False):
            _, _, _, _, matches=await ambiguous();context=matches[0]['review_context']
            if too_many:context['candidates']*=33
            else:context['incoming']['description']='原文'*65_000
            reviewer=AsyncMock()
            with self.assertRaises(ValueError):await resolve_matches([{}],matches,reviewer)
            reviewer.assert_not_awaited()


class AIWriterTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.source, _, self.detail, self.client, _=await ambiguous()
        self.journal=Journal();self.events=verified_events([self.source],[remote(self.source)],USER,self.journal)
        async def read(lines):return await inspect_imports(self.client,lines)
        self.adapter=SimpleNamespace(calendar_import_matches=read,commit=AsyncMock(return_value={'event_id':'new','detail_verified':True}))
        self.reviewer=AsyncMock(return_value=response())

    async def test_preview_ai_is_read_only_confirm_reuses_fresh_decision_and_persists(self):
        await check_existing_events(self.events,self.journal,self.adapter.calendar_import_matches,reviewer=self.reviewer)
        self.assertIsNone(self.journal.status(self.events[0]['key']))
        result=await write_events(self.adapter,self.events,self.journal,reviewer=self.reviewer)
        self.assertEqual(result,{'created':0,'skipped':1});self.reviewer.assert_awaited_once();self.adapter.commit.assert_not_awaited()
        self.assertEqual(self.journal.status(self.events[0]['key']),'done')
        self.assertNotIn('receipt',self.events[0])

    async def test_distinct_new_work_is_committed_once(self):
        self.detail[8]=js_escape('材料品質檢驗')
        self.detail[14]=js_escape(self.source['Description']+'\n材料品質檢驗')
        self.reviewer.return_value=response('distinct',existing='材料品質檢驗')
        self.assertEqual(await write_events(self.adapter,self.events,self.journal,reviewer=self.reviewer),{'created':1,'skipped':0})
        self.adapter.commit.assert_awaited_once()

    async def test_same_original_work_with_quoted_project_and_absent_executor_never_commits(self):
        source, _, _, client, _, answer = await reported_aliases()
        events = verified_events([source], [remote(source)], USER, self.journal)
        async def read(lines):
            return await inspect_imports(client, lines)
        adapter = SimpleNamespace(calendar_import_matches=read, commit=AsyncMock())
        with self.assertRaises(ValueError):
            await write_events(adapter, events, self.journal, reviewer=AsyncMock(return_value=answer))
        adapter.commit.assert_not_awaited()
        self.assertIsNone(self.journal.status(events[0]['key']))

    async def test_failure_and_uncertainty_leave_no_reservation(self):
        self.reviewer.side_effect=TimeoutError('model unavailable')
        with self.assertRaises(TimeoutError):await write_events(self.adapter,self.events,self.journal,reviewer=self.reviewer)
        self.reviewer.side_effect=None;self.reviewer.return_value=response('uncertain')
        with self.assertRaises(ValueError):await write_events(self.adapter,self.events,self.journal,reviewer=self.reviewer)
        self.assertIsNone(self.journal.status(self.events[0]['key']));self.adapter.commit.assert_not_awaited()

    async def test_revocation_during_model_wait_prevents_receipt_or_write(self):
        active = True
        async def review(request):
            nonlocal active
            active = False
            return response()
        with self.assertRaises(ValueError):
            await write_events(self.adapter, self.events, self.journal, lambda: active, reviewer=review)
        self.assertIsNone(self.journal.status(self.events[0]['key']))
        self.adapter.commit.assert_not_awaited()

    async def test_unknown_write_never_calls_ai(self):
        self.journal.reserve_event(self.events[0]['key'],self.events[0]['aliases'])
        with self.assertRaises(ValueError):await write_events(self.adapter,self.events,self.journal,reviewer=self.reviewer)
        self.reviewer.assert_not_awaited();self.adapter.commit.assert_not_awaited()
