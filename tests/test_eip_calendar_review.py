from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from official_document_workflow.application.calendar_review import consolidate_events
from official_document_workflow.application.calendar_review import review_calendar
from official_document_workflow.application.prompts import REVIEW_PROMPT

SOURCES = {'S1': SimpleNamespace(text='原始資料\n完整時間條款')}
EVIDENCE = [{'source_id': 'S1', 'start_line': 1, 'end_line': 2}]


class CalendarReviewTests(IsolatedAsyncioTestCase):
    async def test_keep_cannot_assert_a_date_missing_from_its_citations(self):
        sources = {'S1': SimpleNamespace(text='維修組處理缺失。\n品管組於117年6月12日前提報結果。')}
        events = [
            {'status': 'current', 'start_date': '2028-06-12'},
            {'status': 'current', 'start_date': '2028-06-12'},
        ]
        checks = [{
            'index': i, 'decision': 'keep', 'reason': '已確認日期', 'target': None,
            'evidence_spans': [{'source_id': 'S1', 'start_line': i+1, 'end_line': i+1}],
        }
            for i in range(2)]
        pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value={'checks': checks, 'issues': []}))
        result = await review_calendar(
            pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
            {'sources': []}, events, sources,
        )
        self.assertTrue(result['approved'])
        self.assertEqual([c['decision'] for c in result['checks']], ['reject', 'keep'])

    async def test_detailed_rejection_remains_a_rejection_not_a_format_error(self):
        response = {'checks': [], 'issues': ['原文與候選責任及時間不符。'*50]}
        pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value=response))
        module = SimpleNamespace(CloudSemanticSource=SimpleNamespace)
        result = await review_calendar(pipe, module, {'sources': []}, [], SOURCES)
        self.assertFalse(result['approved'])
        self.assertEqual(result['issues'], response['issues'])

    async def test_merge_cannot_revive_target_with_unproven_date(self):
        checks = [
            {'index': 0, 'decision': 'keep', 'target': None, 'reason': '可排程', 'evidence_spans': EVIDENCE},
            {'index': 1, 'decision': 'merge', 'target': 0, 'reason': '同一成果', 'evidence_spans': EVIDENCE},
        ]
        pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value={'checks': checks, 'issues': []}))
        result = await review_calendar(
            pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
            {'sources': []}, [{'status': 'current', 'start_date': '2028-06-12'}]*2, SOURCES,
        )
        self.assertFalse(result['approved'])
        self.assertEqual([c['decision'] for c in result['checks']], ['reject', 'reject'])

    async def test_independent_context_contains_sources_not_previous_verdict(self):
        pipe = SimpleNamespace(
            _cloud_ocr_text_request=AsyncMock(
                return_value={
                    'checks': [{'index': 0, 'decision': 'keep', 'reason': '原文支持', 'target': None, 'evidence_spans': EVIDENCE}], 'issues': [],
                },
            ),
        )
        module = SimpleNamespace(CloudSemanticSource=SimpleNamespace)
        request = {
            'sources': [{'source_id': 'S1', 'lines': [[1, '原始資料']]}],
            'correction': {'errors': ['previous failure']},
            'all_candidates': [{'responsible_party': 'unverified generated label'}],
        }
        events = [{'status': 'current', 'title': '候選'}, {'status': 'historical'}]
        self.assertTrue((await review_calendar(pipe, module, request, events, SOURCES))['approved'])
        sent = json.loads(pipe._cloud_ocr_text_request.call_args.args[0].text)
        self.assertEqual(sent, {'sources': request['sources'], 'events': events[:1]})
        self.assertNotIn('previous failure', str(sent))
        self.assertEqual(pipe._cloud_ocr_text_request.call_args.kwargs['prompt'], REVIEW_PROMPT)

    async def test_unknown_inconsistent_or_failed_review_is_not_approval(self):
        module = SimpleNamespace(CloudSemanticSource=SimpleNamespace)
        for response in (
            None, {}, {'approved': 'true', 'issues': []},
            {'approved': True, 'issues': ['未能確認']}, {'approved': False, 'issues': []},
            {'approved': False, 'issues': [42]},
        ):
            pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value=response))
            with self.subTest(response=response), self.assertRaises((ValueError, TypeError, AttributeError)):
                await review_calendar(pipe, module, {'sources': []}, [], SOURCES)
            pipe._cloud_ocr_text_request.assert_awaited_once()

    async def test_classifications_require_complete_unambiguous_coverage(self):
        module = SimpleNamespace(CloudSemanticSource=SimpleNamespace)
        keep = {'index': 0, 'decision': 'keep', 'reason': '日期原文支持', 'evidence_spans': EVIDENCE}
        merge = {'index': 1, 'decision': 'merge', 'target': 0, 'reason': '同一成果前置工作', 'evidence_spans': EVIDENCE}
        for checks in (
            [keep], [keep, keep], [dict(keep, index=True), merge],
            [dict(keep, target=1), merge], [keep, dict(merge, target=1)],
            [dict(keep, decision='merge', target=1), merge],
        ):
            pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value={'checks': checks, 'issues': []}))
            with self.subTest(checks=checks), self.assertRaises(ValueError):
                await review_calendar(pipe, module, {'sources': []}, [{'status': 'current'}]*2, SOURCES)

    async def test_reject_cannot_be_overridden_by_global_approval(self):
        pipe = SimpleNamespace(
            _cloud_ocr_text_request=AsyncMock(
                return_value={
                    'approved': True, 'issues': [],
                    'checks': [{'index': 0, 'decision': 'reject', 'reason': '日期指定其他工作', 'evidence_spans': EVIDENCE}],
                },
            ),
        )
        result = await review_calendar(
            pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
            {'sources': []}, [{'status': 'current'}], SOURCES,
        )
        self.assertFalse(result['approved'])
        self.assertIn('日期指定其他工作', result['issues'][0])

    async def test_null_optional_field_cannot_be_the_only_rejection(self):
        check = {
            'index': 0,
            'decision': 'reject',
            'target': None,
            'reason': 'responsible_party 未填',
            'blocking_fields': ['responsible_party'],
            'evidence_spans': EVIDENCE,
        }
        pipe = SimpleNamespace(
            _cloud_ocr_text_request=AsyncMock(
                return_value={'checks': [check], 'issues': []},
            ),
        )
        result = await review_calendar(
            pipe,
            SimpleNamespace(CloudSemanticSource=SimpleNamespace),
            {'sources': []},
            [{'status': 'current', 'responsible_party': None}],
            SOURCES,
        )
        self.assertTrue(result['approved'])
        self.assertEqual(result['checks'][0]['decision'], 'keep')

    async def test_independent_keep_survives_rejection_but_not_global_conflict(self):
        checks = [
            {'index': 0, 'decision': 'reject', 'target': None, 'reason': '日期指定其他工作', 'evidence_spans': EVIDENCE},
            {'index': 1, 'decision': 'keep', 'target': None, 'reason': '獨立期限明確', 'evidence_spans': EVIDENCE},
        ]
        for issues in ([], ['文件版本效力互相矛盾']):
            pipe = SimpleNamespace(_cloud_ocr_text_request=AsyncMock(return_value={'checks': checks, 'issues': issues}))
            result = await review_calendar(
                pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
                {'sources': []}, [{'status': 'current'}]*2, SOURCES,
            )
            self.assertEqual(result['approved'], not issues)
            self.assertEqual(result['global_issues'], issues)
            self.assertIn('日期指定其他工作', str(result['issues']))
            self.assertEqual(result['checks'], checks)

    async def test_every_decision_requires_resolvable_original_evidence(self):
        for decision in ('keep', 'reject'):
            for evidence in (
                None, [], [{'source_id': 'unknown', 'start_line': 1, 'end_line': 2}],
                [{'source_id': 'S1', 'start_line': 1, 'end_line': 4}],
                [{'source_id': 'S1', 'start_line': True, 'end_line': 2}],
            ):
                pipe = SimpleNamespace(
                    _cloud_ocr_text_request=AsyncMock(
                        return_value={
                            'checks': [{
                                'index': 0, 'decision': decision, 'reason': '不能只相信這段說明',
                                'evidence_spans': evidence,
                            }], 'issues': [],
                        },
                    ),
                )
                with self.subTest(decision=decision, evidence=evidence), self.assertRaises((ValueError, TypeError)):
                    await review_calendar(
                        pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
                        {'sources': []}, [{'status': 'current'}], SOURCES,
                    )
                pipe._cloud_ocr_text_request.assert_awaited_once()

    async def test_review_citations_are_not_an_ordered_reconstruction(self):
        evidence = [
            {'source_id': 'S1', 'start_line': 2, 'end_line': 2},
            {'source_id': 'S1', 'start_line': 1, 'end_line': 2},
        ]
        pipe = SimpleNamespace(
            _cloud_ocr_text_request=AsyncMock(
                return_value={
                    'checks': [{
                        'index': 0, 'decision': 'keep', 'reason': '兩處原文共同支持',
                        'evidence_spans': evidence,
                    }], 'issues': [],
                },
            ),
        )
        result = await review_calendar(
            pipe, SimpleNamespace(CloudSemanticSource=SimpleNamespace),
            {'sources': []}, [{'status': 'current'}], SOURCES,
        )
        self.assertTrue(result['approved'])
        self.assertEqual(result['checks'][0]['evidence_spans'], evidence)

    def test_merge_keeps_deadline_owner_and_all_source_clauses(self):
        first = {
            'status': 'current', 'project_name': '甲工程', 'start_date': '2027-04-18',
            'action': '前置修復', 'responsible_party': '維運團隊',
            'description_spans': [{'source_id': 'S1', 'start_line': 1, 'end_line': 3}],
        }
        second = {
            **first, 'action': '彙送成果', 'responsible_party': '管理處',
            'description_spans': [{'source_id': 'S1', 'start_line': 3, 'end_line': 6}],
        }
        checks = [{'index': 0, 'decision': 'merge', 'target': 1}, {'index': 1, 'decision': 'keep'}]
        result = consolidate_events([first, second], checks)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['responsible_party'], '管理處')
        self.assertEqual(result[0]['action'], '彙送成果')
        self.assertEqual(result[0]['description_spans'], [{'source_id': 'S1', 'start_line': 1, 'end_line': 6}])
        self.assertEqual(second['description_spans'][0]['start_line'], 3)
        for field in ('project_name', 'start_date', 'start_time', 'end_date', 'end_time'):
            with self.subTest(field=field), self.assertRaises(ValueError):
                consolidate_events([first, {**second, field: 'different'}], checks)

    def test_independent_same_day_deliverables_are_retained(self):
        events = [{'status': 'current', 'action': '檢測報告'}, {'status': 'current', 'action': '演練紀錄'}]
        checks = [{'index': i, 'decision': 'keep'} for i in range(2)]
        self.assertEqual(consolidate_events(events, checks), events)
