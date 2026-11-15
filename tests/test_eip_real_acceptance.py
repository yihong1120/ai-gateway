"""The acceptance grader must fail unsafe outputs, not merely model errors."""
from __future__ import annotations

import copy
import unittest

from tests.eip_real_acceptance import check_case


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            'document': {'subject': 'fixture'}, 'analysis_status': 'validated',
            'semantic_review': {'approved': True}, 'payload': {'events': []},
            'rows': [{
                'Subject': '交付成果[AI產生]', 'Start Date': '2026/09/09',
                'Start Time(24hr)': '', 'Private': '真',
            }],
        }
        self.preview = {
            'accepted': True, 'projects': ['matched'],
            'replay': 'checked', 'journal_states': ['done'],
        }

    def test_existing_receipt_and_accepted_project_pass(self):
        self.assertTrue(check_case('59166', self.result, self.preview)['passed'])

    def test_blocking_preview_or_missing_receipt_is_not_acceptance(self):
        for field, value in [
            ('accepted', False), ('projects', ['ambiguous']),
            ('replay', 'blocked'), ('journal_states', [None]),
            ('journal_states', ['unknown']),
        ]:
            with self.subTest(field=field, value=value):
                preview = {**self.preview, field: value}
                self.assertFalse(check_case('59166', self.result, preview)['passed'])

    def test_unmatched_project_is_accepted_as_private_fallback(self):
        self.assertTrue(
            check_case(
                '59166', self.result,
                {**self.preview, 'projects': ['unmatched']},
            )['passed'],
        )

    def test_model_approval_cannot_override_wrong_date_or_privacy(self):
        for field, value in [
            ('Start Date', '2026/09/01'), ('Private', '假'),
            ('Subject', '交付成果'),
        ]:
            with self.subTest(field=field):
                result = copy.deepcopy(self.result)
                result['rows'][0][field] = value
                self.assertFalse(check_case('59166', result)['passed'])

    def test_missing_result_is_not_a_successful_no_event_case(self):
        self.assertFalse(check_case('59187', {})['passed'])
