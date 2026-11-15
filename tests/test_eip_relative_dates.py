"""Relative reminders retain source proof and explicitly label estimated dates."""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

from . import test_eip_group_processing as fixtures
from examples.official_document_workflow.application import calendar_review
from examples.official_document_workflow.domain.calendar.contracts import (
    CALENDAR_REVIEW_SCHEMA,
)
from examples.official_document_workflow.domain.calendar.relative_dates import add_days
from examples.official_document_workflow.domain.calendar.relative_dates import ESTIMATE_NOTICE
from examples.official_document_workflow.domain.calendar.relative_dates import period_matches
from examples.official_document_workflow.domain.calendar.relative_dates import ReceiptContext
from examples.shared.runtime.modules import ModuleReference

_workflow_application = ModuleReference('examples.official_document_workflow.application')

REFERENCE = fixtures.payload()['document']['reference_number']


def spec(**changes):
    return {
        'anchor': 'receipt', 'days': 7, 'day_type': 'calendar_days',
        'include_anchor_day': False, **changes,
    }


class RelativeDeadlineTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.GroupTests.setUp
    run_group = fixtures.GroupTests.run_group

    def prepare(self, clause='請於文到後七日內提送評估報告。', *, status='current', relative=None):
        data = fixtures.payload()
        event = data['events'][0]
        event.update(status=status, start_date=None, tentative_schedule=None)
        event['assignment'] = {
            'executor': None, 'date_basis': 'relative', 'anchor_date': None,
            'relative_deadline': spec() if relative is None else relative,
        }
        self.sources['S2'] = fixtures.M.CloudSemanticSource(clause, 'native_pdf_text')
        self.pipe.semantic_source.side_effect = list(self.sources.values())
        self.pipe.analyze_text.return_value = data
        return data

    def saved_rows(self):
        request = next(
            c.kwargs for c in self.pipe.store_output.await_args_list
            if c.kwargs['kind'] == 'calendar_csv'
        )
        return list(csv.DictReader(io.StringIO(request['payload'].decode('utf-8-sig'))))

    async def test_verified_matching_receipt_date_is_used_before_issue_date(self):
        self.prepare()
        self.pipe._eip_receipt_context = ReceiptContext('10', REFERENCE, '2026-09-01')
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        row = self.saved_rows()[0]
        self.assertEqual(row['Start Date'], '2026/09/08')
        self.assertIn('【期限推估】', row['Subject'])
        self.assertIn(ESTIMATE_NOTICE, row['Description'])
        self.assertIn('EIP 收文日期 2026-09-01', row['Description'])
        self.assertEqual(result['date_estimates'][0]['deadline'], '2026-09-08')
        self.pipe.analyze_text.assert_awaited_once()

    async def test_missing_receipt_uses_verified_issue_date_with_explicit_estimate(self):
        self.prepare()
        result = await self.run_group()
        self.assertTrue(result['calendar_ready'], result['errors'])
        row = self.saved_rows()[0]
        self.assertEqual(row['Start Date'], '2026/08/20')
        self.assertIn('暫代送達日', row['Description'])
        self.assertTrue(result['date_estimates'][0]['estimated'])

    async def test_calendar_only_recheck_does_not_render_existing_pdf_or_word_again(self):
        self.prepare()
        self.pipe._eip_calendar_only = True
        result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual([f['kind'] for f in result['files']], ['calendar_csv'])
        self.pipe.render_word.assert_not_called()
        self.pipe.plan_current_user_request.assert_not_awaited()

    async def test_other_document_receipt_cannot_be_borrowed(self):
        self.prepare()
        self.pipe._eip_receipt_context = ReceiptContext('10', '另一件文號', '2026-09-01')
        await self.run_group()
        row = self.saved_rows()[0]
        self.assertEqual(row['Start Date'], '2026/08/20')
        self.assertIn('暫代送達日', row['Description'])
        self.assertNotIn('EIP 收文日期', row['Description'])

    async def test_unapproved_or_cancelled_work_does_not_become_current(self):
        self.prepare(status='needs_review')
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['date_estimates'], [])

    async def test_semantic_rejection_still_blocks_an_estimated_current_candidate(self):
        self.prepare()
        self.review.return_value = {'approved': False, 'checks': [], 'issues': ['本項提案尚未核定。']}
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['calendar_ready'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(result['date_estimates'], [])

    async def test_model_cannot_invent_period_or_day_type(self):
        self.prepare(relative=spec(days=8))
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])
        self.assertEqual(self.pipe.analyze_text.await_count, 2)

    async def test_model_cannot_erase_an_explicit_event_interval(self):
        data = self.prepare()
        data['events'][0].update(start_date='2026-08-20', end_date='2026-08-25')
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])

    async def test_independent_review_receives_audit_and_checks_relative_quote(self):
        analysis = self.prepare()
        self.pipe._eip_receipt_context = ReceiptContext('10', REFERENCE, '2026-09-01')
        requests = []

        async def model(source, *, schema, **kwargs):
            data = json.loads(source.text)
            if schema != CALENDAR_REVIEW_SCHEMA:
                return copy.deepcopy(analysis)
            requests.append(data)
            return {
                'issues': [], 'checks': [{
                    'index': 0, 'decision': 'keep', 'target': None,
                    'reason': '本項工作及七日期間與原文一致，日期已標示推估。',
                    'evidence_spans': [{'source_id': 'S2', 'start_line': 1, 'end_line': 1}],
                }],
            }

        self.pipe.analyze_text.side_effect = model
        with patch(
            'examples.official_document_workflow.application.multi_agent.review_calendar',
            side_effect=calendar_review.review_calendar,
        ):
            result = await self.run_group()
        self.assertTrue(result['complete'], result['errors'])
        self.assertEqual(requests[0]['date_calculations'][0]['anchor_source'], 'EIP 收文日期')
        self.assertNotIn('_relative_date_proof', requests[0]['events'][0])

    async def test_json_object_cannot_forge_a_trusted_date_proof(self):
        data = fixtures.payload()
        data['events'][0]['start_date'] = '2099-01-01'
        data['events'][0]['_relative_date_proof'] = {'deadline': '2099-01-01'}
        self.pipe.analyze_text.return_value = data
        result = await self.run_group()
        self.assertFalse(result['complete'])
        self.assertFalse(result['csv_ids'])

    def test_period_grammar_preserves_units_and_start_day(self):
        self.assertTrue(period_matches('文到後七日內完成', spec()))
        self.assertTrue(period_matches('文到後一週內完成', spec()))
        self.assertTrue(period_matches('函到後柒日內完成', spec()))
        self.assertTrue(period_matches('文到後七日內完成（不含當日）', spec()))
        self.assertFalse(period_matches('發文日期115年10月7日', spec(anchor='issue')))
        self.assertFalse(period_matches('文到後七個工作日內完成', spec()))
        self.assertTrue(period_matches('文到後七個工作日內完成', spec(day_type='working_days')))
        self.assertTrue(period_matches('文到後七個工作天內完成', spec(day_type='working_days')))
        self.assertFalse(period_matches('文到後七日內完成', spec(include_anchor_day=True)))
        self.assertFalse(period_matches('文到後七日內完成；另收文後三日須回報', spec()))

    def test_business_day_estimate_only_skips_weekends_and_honors_inclusive_rule(self):
        self.assertEqual(add_days('2026-09-04', spec(days=1, day_type='working_days')), '2026-09-07')
        self.assertEqual(add_days('2026-09-04', spec(days=1)), '2026-09-05')
        self.assertEqual(add_days('2026-09-04', spec(days=1, include_anchor_day=True)), '2026-09-04')

    def test_form_date_validation_does_not_treat_folder_timestamp_as_receipt(self):
        context = ReceiptContext.from_detail(
            '10', {
                'fields': [
                    {'label': '發文字號', 'value': REFERENCE},
                    {'label': '收文日期', 'value': '115年9月1日'},
                ],
            },
        )
        self.assertEqual(context.received_date, '2026-09-01')
        missing = ReceiptContext.from_detail(
            '10', {
                'fields': [
                    {'label': '文件匣時間', 'value': '115年9月1日'},
                ],
            },
        )
        self.assertIsNone(missing.received_date)


class RetainedExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuse_requires_owner_source_set_and_identical_bytes(self):
        integration = _workflow_application.attribute('integration')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.pdf'
            path.write_bytes(b'%PDF-original')
            item = types.SimpleNamespace(path=str(path), meta={'eip_group_source_ids': ['source']})
            lookup = AsyncMock(return_value=item)
            exports = [{'id': 'export', 'kind': 'document_pdf', 'name': 'document.pdf'}]
            hashes = {'export': hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(integration, 'Files', types.SimpleNamespace(get_file_by_id_and_user_id=lookup)), \
                    patch.object(integration, 'Storage', types.SimpleNamespace(get_file=lambda p: p)):
                self.assertEqual(await integration.retained_document_exports('alice', exports, ['source'], hashes), exports)
                lookup.assert_awaited_with('export', 'alice')
                with self.assertRaises(ValueError):
                    await integration.retained_document_exports('alice', exports, ['other-source'], hashes)
                path.write_bytes(b'%PDF-changed')
                with self.assertRaises(ValueError):
                    await integration.retained_document_exports('alice', exports, ['source'], hashes)
                lookup.return_value = None
                with self.assertRaises(ValueError):
                    await integration.retained_document_exports('bob', exports, ['source'], hashes)
