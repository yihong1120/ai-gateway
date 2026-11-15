from __future__ import annotations

import unittest
from unittest.mock import patch

from .test_eip_connector import csv_bytes
from .test_eip_connector import row
from .test_eip_connector import SCHEMA
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.domain.calendar.presentation import format_ai_title
from examples.official_document_workflow.domain.calendar.presentation import split_ai_title
from examples.official_document_workflow.infrastructure.eip import calendar_csv


class CalendarTitleTests(unittest.TestCase):
    def test_requested_format_exactly(self):
        title = '辦理「台3線吳鳳橋改建工程」之115年8月份區域聯防稽核會議'
        expected = title+'[AI產生](台3線吳鳳橋改建工程)'
        actual, notice = format_ai_title(title, '台3線吳鳳橋改建工程')
        self.assertEqual(actual, expected)
        self.assertFalse(notice)
        self.assertNotIn('\\', actual)
        self.assertNotIn('$', actual)

    def test_idempotent_and_replaces_old_suffix_with_verified_project(self):
        expected = '會勘[AI產生](工地甲)'
        self.assertEqual(format_ai_title(expected, '工地甲'), (expected, ''))
        self.assertEqual(format_ai_title('會勘[AI產生](工地甲)。', '工地乙'), ('會勘[AI產生](工地乙)', ''))
        self.assertEqual(format_ai_title(expected+'[AI產生](工地甲)', '工地甲'), (expected, ''))
        self.assertEqual(format_ai_title('會勘'), ('會勘[AI產生]', ''))

    def test_long_titles_preserve_suffix_and_complete_title_in_description(self):
        title = '稽核缺失改善並提報複核資料'*12
        formatted, notice = format_ai_title(title, '台3線吳鳳橋改建工程')
        self.assertLessEqual(len(formatted.encode('utf-8')), 200)
        self.assertTrue(formatted.endswith('[AI產生](台3線吳鳳橋改建工程)'))
        self.assertIn(title, notice)
        self.assertEqual(format_ai_title(formatted, '台3線吳鳳橋改建工程'), (formatted, ''))

    def test_import_and_relative_reminder_keep_suffix(self):
        base, _ = format_ai_title('稽核缺失改善'*20, '台3線吳鳳橋改建工程')
        source = {**row(), 'Subject': '七天後追蹤改善進度：'+base}
        before = dict(source)
        converted = calendar_csv.fit_calendar_subject(source)
        self.assertLessEqual(len(converted['Subject'].encode()), 200)
        self.assertTrue(converted['Subject'].endswith('[AI產生](台3線吳鳳橋改建工程)'))
        self.assertIn(source['Subject'], converted['Description'])
        self.assertEqual(source, before)
        self.assertEqual(calendar_csv.fit_calendar_subject(converted), converted)
        with patch.object(integration, 'module', new=SCHEMA):
            rows, _ = calendar_csv.parse_csv(csv_bytes([source]))
        self.assertEqual(rows[0]['Subject'], converted['Subject'])

    def test_invalid_project_argument_is_not_rendered(self):
        for name in ('工地\n注入', '工地[AI產生]'):
            self.assertEqual(format_ai_title('會議', name), ('會議[AI產生]', ''))

    def test_parentheses_in_official_project_name(self):
        formatted, _ = format_ai_title('會議', '工程(第一期)')
        self.assertEqual(split_ai_title(formatted), ('會議', '工程(第一期)'))
        self.assertEqual(format_ai_title(formatted, '工程(第一期)'), (formatted, ''))
