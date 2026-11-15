"""Account-scoped AI follow-up reminders keep their evidence and status clear."""
import copy
import csv
import io
import json
import os
from unittest.mock import patch

from . import test_eip_group_processing as fixtures
from examples.official_document_workflow.application import calendar_review
from examples.official_document_workflow.domain.calendar.contracts import CALENDAR_REVIEW_SCHEMA
from examples.official_document_workflow.domain.calendar.relative_dates import ReceiptContext
from examples.official_document_workflow.domain.calendar.tracking_reminders import enabled_for, TRACKING_NOTICE


class TrackingReminderTests(fixtures.GroupTests):
    async def test_opted_in_hourly_workflow_exports_independently_reviewed_preparation(self):
        from datetime import datetime
        from .test_official_preparation import suggestion
        from examples.official_document_workflow.application.preparation_review import SCHEMA

        class FixedDate(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 1, tzinfo=tz)

        data = fixtures.payload()
        data['events'][0]['preparation_reminders'] = [suggestion(days=8)]
        self.pipe._eip_tracking_reminders = True
        async def model(source, *, schema, **kwargs):
            if schema == SCHEMA:
                return {'checks': [{'index': 0, 'decision': 'keep', 'reason': '原文要求準備報告',
                                    'evidence_spans': [{'source_id': 'S1', 'start_line': 8, 'end_line': 8}]}]}
            return copy.deepcopy(data)
        self.pipe.analyze_text.side_effect = model
        with patch('examples.official_document_workflow.application.multi_agent.datetime', FixedDate):
            result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        rows = self.saved_rows()
        self.assertEqual(len(rows), 2)
        self.assertIn('準備提醒', rows[1]['Subject'])
        self.assertIn('非公文明訂期限', rows[1]['Description'])
        self.assertEqual(rows[1]['Start Date'], '2026/09/02')

    async def test_preparation_review_failure_keeps_the_verified_anchor(self):
        from .test_official_preparation import suggestion
        from examples.official_document_workflow.application.preparation_review import SCHEMA

        data = fixtures.payload()
        data['events'][0]['preparation_reminders'] = [suggestion()]
        self.pipe._eip_tracking_reminders = True
        async def model(source, *, schema, **kwargs):
            if schema == SCHEMA:
                raise ValueError('review unavailable')
            return copy.deepcopy(data)
        self.pipe.analyze_text.side_effect = model
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(len(self.saved_rows()), 1)
        self.assertTrue(any('事前準備提醒未通過' in value for value in result['notices']))

    async def test_rejected_preparation_is_not_added_and_is_reported(self):
        from .test_official_preparation import suggestion
        from examples.official_document_workflow.application.preparation_review import SCHEMA

        data = fixtures.payload()
        data['events'][0]['preparation_reminders'] = [suggestion()]
        self.pipe._eip_tracking_reminders = True
        async def model(source, *, schema, **kwargs):
            if schema == SCHEMA:
                return {'checks': [{'index': 0, 'decision': 'reject', 'reason': '原文未支持該準備項目',
                                    'evidence_spans': [{'source_id': 'S1', 'start_line': 8, 'end_line': 8}]}]}
            return copy.deepcopy(data)
        self.pipe.analyze_text.side_effect = model
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        self.assertEqual(len(self.saved_rows()), 1)
        self.assertTrue(any('部分 AI 事前準備建議未通過' in value for value in result['notices']))

    async def test_necessary_action_does_not_require_urgency_keyword(self):
        self.prepare_tracking('請貴公司提送評估報告。')
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual(len(result['tracking_reminders']), 1)

    def prepare_tracking(self, clause='請貴公司儘速提送評估報告。', *, status='current', days=3):
        data = fixtures.payload()
        event = data['events'][0]
        event.update(status=status, start_date=None, end_date=None, start_time=None, end_time=None)
        event['assignment'] = {'executor': None, 'date_basis': 'unspecified', 'anchor_date': None,
                               'tracking_reminder': {'days_after_anchor': days, 'reason': '需儘速追蹤辦理'}}
        self.sources['S2'] = fixtures.M.CloudSemanticSource(clause, 'native_pdf_text')
        self.pipe.semantic_source.side_effect = list(self.sources.values())
        self.pipe.analyze_text.return_value = data
        return data

    def saved_rows(self):
        request = next(c.kwargs for c in self.pipe.store_output.await_args_list if c.kwargs['kind']=='calendar_csv')
        return list(csv.DictReader(io.StringIO(request['payload'].decode('utf-8-sig'))))

    async def test_matching_receipt_date_creates_labeled_reminder_and_audit(self):
        self.prepare_tracking()
        self.pipe._eip_tracking_reminders = True
        reference = fixtures.payload()['document']['reference_number']
        self.pipe._eip_receipt_context = ReceiptContext('10', reference, '2026-09-01')
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        row = self.saved_rows()[0]
        self.assertEqual(row['Start Date'], '2026/09/04')
        self.assertIn('【追蹤提醒】', row['Subject'])
        self.assertIn(TRACKING_NOTICE, row['Description'])
        self.assertIn('EIP 收文日期 2026-09-01', row['Description'])
        self.assertEqual(result['date_estimates'], [])
        self.assertEqual(result['tracking_reminders'][0]['reminder_date'], '2026-09-04')

    async def test_unknown_receipt_uses_issue_date_and_labels_assumption(self):
        self.prepare_tracking()
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertIn('收文日未確認', self.saved_rows()[0]['Description'])
        self.assertFalse(result['tracking_reminders'][0]['receipt_date_recorded'])

    async def test_disabled_account_keeps_missing_date_pending(self):
        self.prepare_tracking()
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertEqual(result['csv_ids'], [])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_unapproved_work_cannot_be_promoted(self):
        self.prepare_tracking(status='needs_review')
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertEqual(result['csv_ids'], [])

    async def test_explicit_relative_period_is_not_replaced_by_reminder(self):
        self.prepare_tracking('請於文到後五日內提送評估報告。')
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_financial_chinese_period_is_not_replaced_by_reminder(self):
        self.prepare_tracking('請於函到後柒日內提送評估報告。')
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_explicit_calendar_date_is_not_replaced_by_reminder(self):
        self.prepare_tracking('請於115年10月16日提送評估報告。')
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_wrong_document_receipt_is_never_used_as_anchor(self):
        self.prepare_tracking()
        self.pipe._eip_tracking_reminders = True
        self.pipe._eip_receipt_context = ReceiptContext('10', '另一件文號', '2026-09-01')
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertIn('收文日未確認', self.saved_rows()[0]['Description'])
        self.assertFalse(result['tracking_reminders'][0]['receipt_date_recorded'])

    async def test_semantic_rejection_blocks_tracking_reminder(self):
        self.prepare_tracking()
        self.pipe._eip_tracking_reminders = True
        self.review.return_value = {'approved': False, 'checks': [], 'issues': ['本項提案尚未核定。']}
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_independent_review_rejects_notice_without_followup(self):
        data = self.prepare_tracking('函送相關資料供貴公司知悉，請查照。')
        self.pipe._eip_tracking_reminders = True
        async def model(source, *, schema, **kwargs):
            if schema != CALENDAR_REVIEW_SCHEMA:
                return copy.deepcopy(data)
            return {'issues': [], 'checks': [{'index': 0, 'decision': 'reject', 'target': None,
                    'reason': '僅供知悉，沒有需追蹤的具體工作。',
                    'evidence_spans': [{'source_id': 'S2', 'start_line': 1, 'end_line': 1}]}]}
        self.pipe.analyze_text.side_effect = model
        with patch('examples.official_document_workflow.application.multi_agent.review_calendar', side_effect=calendar_review.review_calendar):
            result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['tracking_reminders'], [])

    async def test_out_of_range_suggestion_is_rejected(self):
        self.prepare_tracking(days=0)
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['validation_issues'][0]['code'], 'event_fields_unverified')
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_followup_interval_is_not_limited_to_thirty_days(self):
        self.prepare_tracking(days=45)
        self.pipe._eip_tracking_reminders = True
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual(result['tracking_reminders'][0]['days_after_anchor'], 45)

    async def test_independent_review_uses_source_evidence_not_forged_date(self):
        data = self.prepare_tracking()
        self.pipe._eip_tracking_reminders = True
        calls = []
        async def model(source, *, schema, **kwargs):
            request = json.loads(source.text)
            if schema != CALENDAR_REVIEW_SCHEMA:
                return copy.deepcopy(data)
            calls.append(request)
            return {'issues': [], 'checks': [{'index': 0, 'decision': 'keep', 'target': None,
                    'reason': '工作成立，日期為追蹤建議。',
                    'evidence_spans': [{'source_id': 'S2', 'start_line': 1, 'end_line': 1}]}]}
        self.pipe.analyze_text.side_effect = model
        with patch('examples.official_document_workflow.application.multi_agent.review_calendar', side_effect=calendar_review.review_calendar):
            result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual(calls[0]['tracking_reminders'][0]['kind'], 'tracking_reminder')
        self.assertNotIn('_tracking_reminder_proof', calls[0]['events'][0])

    async def test_independent_review_must_cite_the_same_action_source(self):
        data = self.prepare_tracking('請貴公司儘速提送評估報告。\n其他單位曾提送評估報告。')
        self.pipe._eip_tracking_reminders = True
        async def model(source, *, schema, **kwargs):
            if schema != CALENDAR_REVIEW_SCHEMA:
                return copy.deepcopy(data)
            return {'issues': [], 'checks': [{'index': 0, 'decision': 'keep', 'target': None,
                    'reason': '工作成立，日期為追蹤建議。',
                    'evidence_spans': [{'source_id': 'S2', 'start_line': 2, 'end_line': 2}]}]}
        self.pipe.analyze_text.side_effect = model
        with patch('examples.official_document_workflow.application.multi_agent.review_calendar', side_effect=calendar_review.review_calendar):
            result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['tracking_reminders'], [])

    def test_owner_allowlist_is_exact_and_off_by_default(self):
        with patch.dict(os.environ, {'EIP_TRACKING_REMINDER_ACCOUNTS':'owner-a:17, owner-b:42'}):
            self.assertTrue(enabled_for('owner-a', '17'))
            self.assertFalse(enabled_for('owner-a', '18'))
            self.assertFalse(enabled_for('owner', '17'))
            self.assertFalse(enabled_for('owner-c', '17'))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(enabled_for('owner-a', '17'))

    def test_tracking_reminder_is_not_merged_into_an_explicit_event(self):
        event = {'status': 'current', 'project_name': None, 'start_date': '2026-10-02',
                 'start_time': None, 'end_date': None, 'end_time': None, 'description_spans': []}
        tracking = {**event, '_tracking_reminder_proof': object()}
        checks = [{'index': 0, 'decision': 'keep', 'target': None},
                  {'index': 1, 'decision': 'merge', 'target': 0}]
        with self.assertRaisesRegex(ValueError, '不可合併'):
            calendar_review.consolidate_events([event, tracking], checks)
