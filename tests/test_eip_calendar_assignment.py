"""Source relation boundaries; no business names or dates in production rules."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase

from examples.official_document_workflow.domain.calendar.calendar_semantics import assignment_event
from examples.official_document_workflow.domain.calendar.calendar_semantics import optional_event_fields
from examples.official_document_workflow.domain.calendar.calendar_semantics import verify_event_date


class AssignmentTests(TestCase):
    def setUp(self):
        self.sources = {'S1': SimpleNamespace(text='技術組收到通知後辦理驗證。\n實際送達：116年3月2日')}
        self.event = {
            'status': 'current', 'responsible_party': '技術組',
            'start_date': '2027-03-01', 'start_time': '09:00',
            'end_date': '2027-03-01', 'end_time': '10:00',
            'assignment': {
                'time_expression': '收到通知後', 'executor': '技術組',
                'date_basis': 'relative', 'anchor_date': None,
            },
            'evidence_spans': {
                'action': [{'source_id': 'S1', 'start_line': 1, 'end_line': 1}],
                'start_date': [{'source_id': 'S1', 'start_line': 1, 'end_line': 1}],
                'anchor_date': [{'source_id': 'S1', 'start_line': 2, 'end_line': 2}],
            },
        }

    def test_missing_anchor_retains_pending_without_mutating_candidate(self):
        before = deepcopy(self.event)
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['status'], 'needs_review')
        for field in ('start_date', 'start_time', 'end_date', 'end_time'):
            self.assertIsNone(result[field])
        self.assertEqual(self.event, before)
        self.assertEqual(result['assignment'], before['assignment'])

    def test_unspecified_never_borrows_document_date(self):
        self.event['assignment']['date_basis'] = 'unspecified'
        self.assertIsNone(assignment_event(self.event, self.sources)['start_date'])

    def test_known_anchor_does_not_calculate_or_approve_event_date(self):
        self.event['assignment']['anchor_date'] = '2027-03-02'
        self.assertEqual(assignment_event(self.event, self.sources), self.event)

    def test_false_anchor_evidence_is_rejected(self):
        self.event['assignment']['anchor_date'] = '2027-03-03'
        with self.assertRaisesRegex(ValueError, '起算日期'):
            assignment_event(self.event, self.sources)

    def test_conflicting_executor_not_silently_reassigned(self):
        self.event['assignment']['executor'] = '管理組'
        with self.assertRaisesRegex(ValueError, '辦理責任'):
            assignment_event(self.event, self.sources)

    def test_reworded_time_condition_cannot_override_cited_source(self):
        self.event['assignment']['time_expression'] = '明天'
        self.assertIsNone(assignment_event(self.event, self.sources)['start_date'])

    def test_relative_words_do_not_become_explicit_calendar_day(self):
        self.event['assignment']['date_basis'] = 'explicit'
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['start_date'])

    def test_single_explicit_day_corrects_roc_conversion_without_inference(self):
        self.event['assignment'].update(date_basis='explicit', time_expression='116年3月2日')
        self.event['evidence_spans']['action'][0]['end_line'] = 2
        self.event['evidence_spans']['start_date'][0].update(start_line=2, end_line=2)
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['start_date'], '2027-03-02')
        self.assertEqual(result['end_date'], '2027-03-02')
        self.assertTrue(verify_event_date(result['start_date'], '116年3月2日'))

    def test_multiple_explicit_days_never_choose_for_model(self):
        self.sources['S1'].text = '安排於116年3月2日或116年3月3日辦理。'
        self.event['assignment']['date_basis'] = 'explicit'
        self.event['evidence_spans']['start_date'] = [{'source_id': 'S1', 'start_line': 1, 'end_line': 1}]
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['start_date'], '2027-03-01')
        self.assertFalse(verify_event_date(result['start_date'], self.sources['S1'].text))

    def test_display_transcription_difference_does_not_block_valid_date(self):
        self.event['assignment'].update(date_basis='explicit', time_expression='在三月一號之前')
        self.sources['S2'] = SimpleNamespace(text='技術組於116年3月1日前交件。')
        self.event['evidence_spans']['start_date'] = [{'source_id': 'S2', 'start_line': 1, 'end_line': 1}]
        self.assertEqual(assignment_event(self.event, self.sources)['status'], 'current')

    def test_time_summary_cannot_supply_missing_source_reference(self):
        self.event['assignment'].update(date_basis='explicit', time_expression='116年3月1日')
        self.event['evidence_spans']['start_date'] = []
        with self.assertRaises(ValueError):
            assignment_event(self.event, self.sources)

    def test_date_in_separate_attachment_is_not_lost(self):
        self.sources['S2'] = SimpleNamespace(text='驗證安排：116年3月1日')
        self.event['assignment'].update(date_basis='explicit', time_expression='116年3月1日')
        self.event['evidence_spans']['start_date'] = [
            {'source_id': 'S2', 'start_line': 1, 'end_line': 1},
        ]
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['start_date'], '2027-03-01')
        self.assertEqual(result['status'], 'current')

    def test_explicit_activity_can_fall_on_document_issue_day(self):
        text = '發文日期：116年3月1日\n驗證安排：116年3月1日'
        self.assertTrue(verify_event_date('2027-03-01', text))
        self.assertFalse(verify_event_date('2027-03-01', text.splitlines()[0]))

    def test_only_administrative_date_retains_task_as_pending(self):
        self.sources['S1'].text = '發文日期：116年3月1日\n請依限改善。'
        self.event['assignment']['date_basis'] = 'explicit'
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['status'], 'needs_review')
        self.assertIsNone(result['start_date'])

    def test_same_day_end_reuses_date_proof_without_overwriting_explicit_proof(self):
        event = deepcopy(self.event)
        event['responsible_party'] = None
        before = deepcopy(event)
        result = optional_event_fields(event, self.sources)
        self.assertEqual(result['evidence_spans']['end_date'], event['evidence_spans']['start_date'])
        self.assertEqual(event, before)
        event['evidence_spans']['end_date'] = [{'source_id': 'S1', 'start_line': 2, 'end_line': 2}]
        self.assertEqual(
            optional_event_fields(event, self.sources)['evidence_spans']['end_date'],
            event['evidence_spans']['end_date'],
        )

    def test_same_day_end_combines_start_date_with_end_time_proof(self):
        event = deepcopy(self.event)
        event['responsible_party'] = None
        self.sources['S2'] = SimpleNamespace(text='散會（上午10時00分）')
        end = {'source_id': 'S2', 'start_line': 1, 'end_line': 1}
        event['evidence_spans']['end_date'] = [end]
        result = optional_event_fields(event, self.sources)
        self.assertEqual(
            result['evidence_spans']['end_date'], [
                *event['evidence_spans']['start_date'], end,
            ],
        )

    def test_explicit_single_day_in_end_field_is_canonicalized_to_start(self):
        self.sources['S2'] = SimpleNamespace(text='管理組於116年3月1日前提報成果。')
        self.event.update(start_date=None, start_time=None, end_date='2027-03-01', end_time=None)
        self.event['assignment'].update(date_basis='explicit', executor='技術組')
        self.event['evidence_spans'].update(
            start_date=[], end_date=[
                {'source_id': 'S2', 'start_line': 1, 'end_line': 1},
            ],
        )
        result = assignment_event(self.event, self.sources)
        self.assertEqual(result['start_date'], '2027-03-01')
        self.assertEqual(result['evidence_spans']['start_date'], result['evidence_spans']['end_date'])

    def test_unique_exact_project_repairs_incomplete_line_range(self):
        self.sources['S2'] = SimpleNamespace(text='工程：青原\n機房更新工程')
        event = deepcopy(self.event)
        event.update(project_name='青原機房更新工程', responsible_party=None)
        event['evidence_spans'].update(project_name=[{'source_id': 'S2', 'start_line': 2, 'end_line': 2}])
        result = optional_event_fields(event, self.sources)
        self.assertEqual(
            result['evidence_spans']['project_name'], [
                {'source_id': 'S2', 'start_line': 1, 'end_line': 2},
            ],
        )

    def test_dropped_unverified_responsible_party_keeps_assignment_consistent(self):
        event = deepcopy(self.event)
        event['evidence_spans']['responsible_party'] = [{'source_id': 'S1', 'start_line': 2, 'end_line': 2}]
        result = optional_event_fields(event, self.sources)
        self.assertIsNone(result['responsible_party'])
        self.assertIsNone(result['assignment']['executor'])

    def test_header_name_does_not_prove_action_executor(self):
        self.sources['S1'].text = '受文者：技術組\n協作節點：116年3月2日完成報告交付。'
        event = deepcopy(self.event)
        event['evidence_spans'].update(
            action=[{'source_id': 'S1', 'start_line': 2, 'end_line': 2}],
            responsible_party=[{'source_id': 'S1', 'start_line': 1, 'end_line': 1}],
        )
        result = optional_event_fields(event, self.sources)
        self.assertIsNone(result['responsible_party'])
        self.assertIsNone(result['assignment']['executor'])

    def test_invalid_relation_is_not_unknown_timing(self):
        self.event['assignment']['date_basis'] = 'guess'
        with self.assertRaisesRegex(ValueError, '格式'):
            assignment_event(self.event, self.sources)

    def test_old_candidates_and_historical_events_are_unchanged(self):
        del self.event['assignment']
        self.assertEqual(assignment_event(self.event, self.sources), self.event)
        self.event.update(status='historical', assignment={})
        self.assertEqual(assignment_event(self.event, self.sources), self.event)
