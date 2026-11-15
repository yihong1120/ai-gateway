"""Analysis/export boundaries keep partial success and independent calendar rows."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from examples.official_document_workflow.application.outcomes import GroupResult
from examples.official_document_workflow.domain.calendar.calendar_semantics import CalendarRules
from examples.official_document_workflow.domain.calendar.rows import CalendarRows


class DocumentOutcomeTests(unittest.TestCase):
    def test_calendar_rows_are_snapshots_and_reminders_create_a_new_result(self):
        state = {
            'group_index': 1,
            'rows': [{'Subject': 'verified'}],
            'semantic_review': {'approved': True},
            'csv_errors': [],
        }
        result = GroupResult.from_analysis(state, ['document exported'])
        state['rows'][0]['Subject'] = 'later mutation'
        state['rows'].append({'Subject': 'other'})
        self.assertEqual(result.calendar.rows[0]['Subject'], 'verified')
        with self.assertRaises(TypeError):
            result.calendar.rows[0]['Subject'] = 'changed'
        updated = result.calendar.with_reminders([{'Subject': 'reminder'}])
        self.assertEqual(len(result.calendar.rows), 1)
        self.assertEqual(len(updated.rows), 2)
        rows = updated.export_rows()
        rows[0]['Subject'] = 'mutable export copy'
        self.assertEqual(updated.rows[0]['Subject'], 'verified')
        self.assertEqual(result.notes, ('document exported',))
        with self.assertRaises(FrozenInstanceError):
            result.document.group_index = 2

    def test_pending_calendar_does_not_erase_the_document_outcome(self):
        document = object()
        state = {
            'group_index': 2,
            'document': document,
            'primary': 'S1',
            'rows': [],
            'csv_errors': ['date needs confirmation'],
        }
        result = GroupResult.from_analysis(state)
        self.assertIs(result.document.document, document)
        self.assertEqual(result.calendar.issues, ('date needs confirmation',))
        self.assertFalse(result.calendar.reviewed)
        self.assertFalse(result.calendar.rows)

    def test_calendar_rules_run_without_processor_request_or_host_state(self):
        rules = CalendarRules(2, CalendarRows(default_private='真'))
        self.assertEqual(rules.events({'events': []}, {}, None), ([], []))
        with self.assertRaises(ValueError):
            rules.events({'events': [{}, {}, {}]}, {}, None)
        self.assertFalse(hasattr(rules, 'request'))
