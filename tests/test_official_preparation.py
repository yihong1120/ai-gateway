"""Suggested preparation dates stay separate from verified document deadlines."""
from __future__ import annotations

import copy
import unittest
from datetime import date

from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import PIPE_MODULE as M
from .test_official_document_processor_pipe import SOURCE
from examples.official_document_workflow.application.calendar_review import consolidate_events
from examples.official_document_workflow.application.group_processing import parse_group
from examples.official_document_workflow.domain.calendar.preparation import preparation_rows
from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv


def suggestion(days=3, action='彙整評估報告資料並核對附件完整性', start='09:30', end='11:00'):
    return {
        'days_before': days, 'action': action,
        'start_time': start, 'end_time': end,
        'reason': '留出資料彙整及檢查時間，避免期限當日才發現缺件。',
        'evidence_spans': [{'source_id': 'S1', 'start_line': 8, 'end_line': 8}],
    }


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.pipe = M.Pipe()
        self.data = payload(('S1',))
        self.data['events'][0]['preparation_reminders'] = [suggestion()]
        self.sources = {'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text')}
        accepted = []
        primary, document, deadlines, rows, errors = parse_group(
            self.pipe,  self.data, self.sources, accepted_events=accepted,
        )
        self.assertFalse(errors)
        self.analysis = dict(
            primary=primary, document=document, deadlines=deadlines,
            rows=rows, accepted_events=accepted, sources=self.sources,
        )

    def render(self, *, today=date(2026, 9, 1), seen=None):
        return preparation_rows(
            self.pipe.calendar,  self.analysis, today=today,
            seen=seen if seen is not None else set(),
        )

    def test_content_action_relative_date_and_csv_contract(self):
        original = copy.deepcopy(self.analysis['rows'])
        rows, notices = self.render()
        self.assertFalse(notices)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['Start Date'], '2026/09/07')
        self.assertEqual(row['End Date'], row['Start Date'])
        self.assertEqual(row['All day event'], '假')
        self.assertEqual(row['Private'], '真')
        self.assertEqual(row['Name'], 'AI填寫')
        self.assertEqual(row['Custom Field4'], '待指派（AI建議）')
        self.assertEqual(row['Start Time'], '09:30:00')
        self.assertEqual(row['Start Time(24hr)'], '09:30')
        self.assertEqual(row['End Time'], '11:00:00')
        self.assertEqual(row['End Time(24hr)'], '11:00')
        self.assertEqual(row['Work Hour'], '1.5')
        self.assertEqual(row['Report Status'], '待辦')
        self.assertEqual(row['Completed'], '0')
        self.assertEqual(row['Custom Field1'], original[0]['Custom Field1'])
        self.assertIn('彙整評估報告資料', row['Subject'])
        self.assertIn('非公文明訂期限', row['Description'])
        self.assertIn('提前 3 天', row['Description'])
        self.assertIn('09:30–11:00', row['Description'])
        self.assertIn('非公文明訂時間', row['Description'])
        self.assertIn(original[0]['Description'], row['Description'])
        self.assertEqual(set(row), set(M.CALENDAR_HEADERS))
        self.assertEqual(self.analysis['rows'], original)
        exported, _ = parse_csv(self.pipe.calendar_csv([*original, row]), csv_format=M.CALENDAR_CSV_FORMAT)
        from examples.official_document_workflow.infrastructure.eip.calendar_csv import canonical_time
        expected = [{
            **value, 'Start Time': canonical_time(value, 'Start'),
            'End Time': canonical_time(value, 'End'),
        } for value in [*original, row]]
        self.assertEqual(exported, expected)

    def test_model_selected_afternoon_time_and_duration_survive_eip_transport(self):
        import csv
        import io
        self.analysis['accepted_events'][0]['preparation_reminders'] = [
            suggestion(start='13:15', end='15:45'),
        ]
        rows, notices = self.render()
        self.assertFalse(notices)
        self.assertEqual(rows[0]['Work Hour'], '2.5')
        exported, transport = parse_csv(self.pipe.calendar_csv(rows), csv_format=M.CALENDAR_CSV_FORMAT)
        self.assertEqual(exported[0]['Description'], rows[0]['Description'])
        self.assertEqual(exported[0]['Start Time(24hr)'], rows[0]['Start Time(24hr)'])
        imported = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))[0]
        self.assertEqual(imported['Name'], 'AI填寫')
        self.assertEqual(imported['Start Time'], '下午01:15:00')
        self.assertEqual(imported['End Time'], '下午03:45:00')
        self.assertEqual(imported['Start Time(24hr)'], '13:15')
        self.assertEqual(imported['End Time(24hr)'], '15:45')

    def test_incomplete_or_invalid_time_skips_suggestion_and_retains_original(self):
        missing = suggestion()
        del missing['end_time']
        original = copy.deepcopy(self.analysis['rows'])
        for invalid in [
            missing, suggestion(start='9:00'), suggestion(end='24:00'),
            suggestion(end='11:99'), suggestion(start=None),
            suggestion(start='11:00', end='11:00'),
            suggestion(start='23:30', end='00:30'),
        ]:
            with self.subTest(invalid=invalid):
                self.analysis['accepted_events'][0]['preparation_reminders'] = [invalid]
                rows, notices = self.render()
                self.assertEqual(rows, [])
                self.assertTrue(notices)
                self.assertEqual(self.analysis['rows'], original)

    def test_fill_unknown_metadata_without_inventing_category_or_assignment(self):
        base = self.analysis['rows'][0]
        base.update({
            'Custom Field3': '', 'Custom Field4': '原承辦公司', 'Custom Field5': '',
            'Categories': '', 'Completed': '100', 'Report Status': '已結案',
        })
        original = copy.deepcopy(base)
        rows, _ = self.render()
        row = rows[0]
        self.assertEqual(row['Custom Field3'], '未載明')
        self.assertEqual(row['Custom Field5'], '未載明')
        self.assertEqual(row['Custom Field4'], '待指派（AI建議）')
        self.assertEqual(row['Completed'], '0')
        self.assertEqual(row['Report Status'], '待辦')
        self.assertEqual([field for field, value in row.items() if not value], ['Categories'])
        self.assertEqual(base, original)
        base.update({
            'Custom Field3': '已驗證受文公司', 'Custom Field5': '已驗證承辦單位',
            'Categories': M.CALENDAR_PROJECT_CATEGORIES[0],
        })
        row = self.render()[0][0]
        for field in ('Custom Field3', 'Custom Field5', 'Categories'):
            self.assertEqual(row[field], base[field])

    def test_distinct_steps_but_no_duplicate_or_cascading_reminder(self):
        self.analysis['accepted_events'][0]['preparation_reminders'] = [
            suggestion(7), suggestion(7, start='13:00', end='15:00'),
            suggestion(1, '完成評估報告送件前最後核對'),
        ]
        seen = set()
        rows, notices = self.render(seen=seen)
        self.assertFalse(notices)
        self.assertEqual([r['Start Date'] for r in rows], ['2026/09/03', '2026/09/09'])
        self.assertEqual(self.render(seen=seen)[0], [])

    def test_expired_suggestion_is_not_shifted_to_an_invented_date(self):
        self.assertEqual(len(self.render(today=date(2026, 9, 7))[0]), 1)
        rows, notices = self.render(today=date(2026, 9, 8))
        self.assertEqual(rows, [])
        self.assertIn('未補排', notices[0])
        self.assertEqual(self.analysis['rows'][0]['Start Date'], '2026/09/10')

    def test_before_issue_date_is_not_scheduled(self):
        self.analysis['accepted_events'][0]['preparation_reminders'] = [suggestion(7)]
        from dataclasses import replace
        issued = replace(self.analysis['document'].issued, gregorian='2026/09/05')
        self.analysis['document'] = replace(self.analysis['document'], issued=issued)
        self.assertEqual(self.render()[0], [])

    def test_pending_or_rejected_anchor_produces_no_reminder(self):
        self.analysis['rows'] = []
        self.assertEqual(self.render(), ([], []))

    def test_bad_evidence_or_offset_does_not_remove_original_event(self):
        bad_span = suggestion()
        bad_span['evidence_spans'][0]['source_id'] = 'OTHER_DOCUMENT'
        for invalid in [
            bad_span, suggestion(0), suggestion(True),
            suggestion(1.5), suggestion('3'), suggestion(action='   '),
        ]:
            with self.subTest(invalid=invalid):
                self.analysis['accepted_events'][0]['preparation_reminders'] = [invalid]
                rows, notices = self.render()
                self.assertEqual(rows, [])
                self.assertTrue(notices)
                self.assertEqual(len(self.analysis['rows']), 1)

    def test_review_merge_preserves_distinct_preparations_without_mutating_input(self):
        first = self.analysis['accepted_events'][0]
        second = copy.deepcopy(first)
        second['preparation_reminders'] = [suggestion(1, '核對提送版本及收件資料')]
        before = copy.deepcopy([first, second])
        merged = consolidate_events(
            [first, second], [
                {'index': 0, 'decision': 'keep', 'target': None},
                {'index': 1, 'decision': 'merge', 'target': 0},
            ],
        )
        self.assertEqual([first, second], before)
        self.analysis['accepted_events'] = merged
        self.assertEqual(len(self.render()[0]), 2)

    def test_shared_project_reminder_is_not_duplicated_across_letters(self):
        # Exact same reviewed event/action in related letters shares a reminder.
        from dataclasses import replace
        event = self.analysis['accepted_events'][0]
        event['project_name'] = '代辦國道3號古坑交流道平面側車道工程'
        event['evidence_spans']['project_name'] = [{'source_id': 'S1', 'start_line': 6, 'end_line': 6}]

        def refresh_rows():
            _, self.analysis['rows'] = self.pipe.calendar.events(  {'events': [event]},
                self.sources, self.analysis['document'],
            )
        refresh_rows()
        seen = set()
        self.assertEqual(len(self.render(seen=seen)[0]), 1)
        self.analysis['document'] = replace(self.analysis['document'], reference_number='關聯函字第1150000002號')
        refresh_rows()
        self.assertEqual(self.render(seen=seen)[0], [])

    def test_month_year_boundary_uses_calendar_days(self):
        event = self.analysis['accepted_events'][0]
        event['start_date'] = '2027-01-03'
        self.sources['S1'] = M.CloudSemanticSource(SOURCE.replace('115年9月10日', '116年1月3日'), 'native_pdf_text')
        event['preparation_reminders'] = [suggestion(7)]
        _, self.analysis['rows'] = self.pipe.calendar.events(  {'events': [event]},
            self.sources, self.analysis['document'],
        )
        rows, notices = self.render(today=date(2026, 12, 20))
        self.assertFalse(notices)
        self.assertEqual(rows[0]['Start Date'], '2026/12/27')


if __name__ == '__main__':
    unittest.main()
