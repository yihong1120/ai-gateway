"""AI time estimates remain separate from evidence and import as complete rows."""
from __future__ import annotations

import csv
import io
import unittest
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import unquote_plus

from .test_eip_group_processing import payload
from .test_official_document_processor_pipe import _deadline
from .test_official_document_processor_pipe import _document
from .test_official_document_processor_pipe import _valid_payload
from .test_official_document_processor_pipe import DEADLINE_EVIDENCE
from .test_official_document_processor_pipe import PIPE_MODULE as M
from .test_official_document_processor_pipe import SOURCE
from examples.official_document_workflow.application.group_processing import parse_group
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
from examples.official_document_workflow.domain.calendar.scheduling import EIP_REVIEW_NOTICE
from examples.official_document_workflow.domain.calendar.scheduling import TENTATIVE_NOTICE
from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
from examples.official_document_workflow.infrastructure.eip.calendar_csv import restore_midnight_reminders


def suggestion(**changes):
    return {
        'start_time': '09:00', 'end_date': '2026-09-10', 'end_time': '11:30',
        'reason': '依公文提送評估報告，暫留兩個半小時整理與核對資料。',
        **changes,
    }


class TentativeScheduleTests(unittest.TestCase):
    def setUp(self):
        self.pipe = M.Pipe()

    def row(self, deadline):
        return self.pipe._calendar_rows(
            owner_name='測試', document=_document(), deadlines=[deadline],
        )[0]

    def roundtrip(self, row):
        download = self.pipe.calendar_csv([row])
        direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        imported, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(len(direct), len(imported))
        for expected, actual in zip(restore_midnight_reminders(direct), imported):
            self.assertEqual(expected['Description'], actual['Description'])
        if '_source_interval' in imported[0]:
            self.assertEqual(imported[0]['_source_interval']['Description'], row['Description'])
        return imported

    def test_date_only_uses_content_estimate_and_retains_original_facts(self):
        original = _deadline()
        deadline = replace(original, tentative_schedule=suggestion())
        row = self.row(deadline)
        self.assertEqual((row['Start Time(24hr)'], row['End Time(24hr)']), ('09:00', '11:30'))
        self.assertEqual(row['Work Hour'], '2.5')
        self.assertEqual(row['All day event'], '假')
        self.assertIn(TENTATIVE_NOTICE, row['Description'])
        self.assertIn('待確認欄位：開始時間、結束日期、結束時間', row['Description'])
        self.assertIn(suggestion()['reason'], row['Description'])
        self.assertIn(EIP_REVIEW_NOTICE, row['Description'])
        self.assertTrue(row['Description'].endswith(_document().explanation))
        self.assertIsNone(deadline.time)
        self.assertIsNone(deadline.end_time)
        self.assertEqual(self.roundtrip(row)[0]['End Time(24hr)'], '11:30')

    def test_start_only_retains_known_time_and_estimates_variable_duration(self):
        deadline = replace(_deadline(timed=True), tentative_schedule=suggestion(end_time='18:00'))
        row = self.row(deadline)
        self.assertEqual(row['Start Time(24hr)'], '15:30')
        self.assertEqual(row['End Time(24hr)'], '18:00')
        self.assertEqual(row['Work Hour'], '2.5')
        self.assertIn('待確認欄位：結束日期、結束時間', row['Description'])
        imported = self.roundtrip(row)[0]
        self.assertNotIn('系統預留', imported['Description'])

    def test_period_hints_can_supply_estimates_without_becoming_exact_source_times(self):
        for period, start, end in (
            ('中午', '12:00', '13:00'), ('下午', '14:00', '16:30'),
        ):
            with self.subTest(period=period):
                data = payload(('S1',))
                reason = f'原文指示於{period}辦理，依提送作業所需時長暫排。'
                data['events'][0]['tentative_schedule'] = suggestion(
                    start_time=start, end_time=end, reason=reason,
                )
                source = SOURCE.replace(
                    DEADLINE_EVIDENCE,
                    DEADLINE_EVIDENCE + f'本次提送作業於{period}辦理。',
                )
                _, _, deadlines, rows, errors = parse_group(
                    self.pipe,  data,
                    {'S1': M.CloudSemanticSource(source, 'native_pdf_text')},
                )
                self.assertEqual(errors, [])
                self.assertIsNone(deadlines[0].time)
                self.assertIsNone(deadlines[0].end_time)
                self.assertEqual(rows[0]['Start Time(24hr)'], start)
                self.assertEqual(rows[0]['End Time(24hr)'], end)
                self.assertIn(reason, rows[0]['Description'])
                self.assertIn(TENTATIVE_NOTICE, rows[0]['Description'])
                self.assertEqual(self.roundtrip(rows[0])[0]['Start Time(24hr)'], start)

    def test_date_without_a_reasonable_estimate_exports_as_all_day(self):
        data = payload(('S1',))
        data['events'][0]['tentative_schedule'] = None
        _, _, deadlines, rows, errors = parse_group(
            self.pipe,  data,
            {'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text')},
        )
        self.assertEqual(errors, [])
        self.assertIsNone(deadlines[0].time)
        row = self.roundtrip(rows[0])[0]
        self.assertEqual(row['All day event'], '真')
        self.assertEqual(row['Start Date'], '2026/09/10')
        self.assertEqual(row['End Date'], '2026/09/10')
        for key in ('Start Time', 'Start Time(24hr)', 'End Time', 'End Time(24hr)'):
            self.assertEqual(row[key], '')
        self.assertNotIn(TENTATIVE_NOTICE, row['Description'])

    def test_end_only_can_estimate_start_without_overwriting_known_end(self):
        data = _valid_payload()
        event = data['events'][0]
        event.update(
            end_time='15:00',
            tentative_schedule=suggestion(start_time='13:30', end_time='16:00'),
        )
        source = SOURCE + '\n本次資料整理於115年9月10日下午3時結束。'
        event['evidence'] = '本次資料整理於115年9月10日下午3時結束。'
        deadline = self.pipe._parse_cloud_ocr_text_payload(
            data, 'test', M.CloudSemanticSource(source, 'native_pdf_text'),
        ).deadlines[0]
        self.assertIsNone(deadline.time)
        self.assertEqual(deadline.end_time.twenty_four_hour, '15:00')
        row = self.row(deadline)
        self.assertEqual((row['Start Time(24hr)'], row['End Time(24hr)']), ('13:30', '15:00'))
        self.assertEqual(self.roundtrip(row)[0]['Work Hour'], '1.5')

    def test_known_end_date_is_preserved(self):
        deadline = replace(
            _deadline(), end_date=M.Pipe._document_date('115', '09', '12'),
            tentative_schedule=suggestion(),
        )
        row = self.row(deadline)
        self.assertEqual(row['End Date'], '2026/09/12')
        self.assertEqual(self.roundtrip(row)[-1]['Start Date'], '2026/09/12')

    def test_complete_source_interval_is_unchanged(self):
        deadline = replace(
            _deadline(timed=True), end_date=_deadline().date,
            end_time=M.DocumentTime('17:00:00', '17:00'),
            tentative_schedule=suggestion(),
        )
        row = self.row(deadline)
        self.assertEqual(row, self.row(replace(deadline, tentative_schedule=None)))
        self.assertNotIn(TENTATIVE_NOTICE, row['Description'])

    def test_estimated_midnight_end_survives_export_and_import_segmentation(self):
        deadline = replace(
            _deadline(timed=True), time=M.DocumentTime('23:30:00', '23:30'),
            tentative_schedule=suggestion(end_date='2026-09-11', end_time='01:00'),
        )
        rows = self.roundtrip(self.row(deadline))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['End Time(24hr)'], '24:00')
        self.assertEqual(rows[1]['Start Date'], '2026/09/11')
        self.assertEqual(rows[1]['End Time(24hr)'], '01:00')
        self.assertIn(EIP_REVIEW_NOTICE, rows[1]['Description'])

    def test_date_only_estimate_can_end_next_day_without_assuming_a_source_end(self):
        deadline = replace(
            _deadline(), tentative_schedule=suggestion(
                start_time='23:00', end_date='2026-09-11', end_time='01:00',
            ),
        )
        row = self.row(deadline)
        self.assertEqual(row['End Date'], '2026/09/11')
        self.assertEqual(row['Work Hour'], '2')
        self.assertEqual(len(self.roundtrip(row)), 2)

    def test_invalid_or_unbounded_estimates_cannot_replace_source(self):
        for proposed in (
            suggestion(end_time='08:00'), suggestion(end_date='2026-09-09'),
            suggestion(end_date='2026-09-13'), suggestion(end_date='2026-02-30'),
            suggestion(start_time='25:00'), suggestion(reason=' '),
        ):
            with self.subTest(proposed=proposed), self.assertRaises(ValueError):
                self.row(replace(_deadline(timed=True), tentative_schedule=proposed))

    def test_reviewed_group_exports_estimate_with_original_evidence_unchanged(self):
        data = payload(('S1',))
        event = data['events'][0]
        event['tentative_schedule'] = suggestion()
        _, _, deadlines, rows, errors = parse_group(
            self.pipe,  data, {'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text')},
        )
        self.assertEqual(errors, [])
        self.assertEqual(self.roundtrip(rows[0])[0]['End Time(24hr)'], '11:30')
        self.assertIsNone(event['start_time'])
        self.assertIsNone(deadlines[0].time)
        self.assertIn(TENTATIVE_NOTICE, rows[0]['Description'])

    def test_estimate_cannot_supply_a_missing_source_date(self):
        data = payload(('S1',))
        data['events'][0].update(start_date=None, status='needs_review', tentative_schedule=suggestion())
        _, _, _, rows, errors = parse_group(
            self.pipe,  data, {'S1': M.CloudSemanticSource(SOURCE, 'native_pdf_text')},
        )
        self.assertEqual(rows, [])
        self.assertTrue(errors)

    def test_legacy_start_only_csv_uses_shared_labelled_reservation_once(self):
        row = self.row(_deadline(timed=True))
        with patch.dict('os.environ', {'EIP_CALENDAR_DEFAULT_DURATION_MINUTES': '120'}):
            imported = self.roundtrip(row)[0]
        self.assertEqual(imported['End Time(24hr)'], '17:30')
        self.assertIn('系統預留 120 分鐘', imported['Description'])
        self.assertIn(EIP_REVIEW_NOTICE, imported['Description'])
        self.assertNotIn(TENTATIVE_NOTICE, imported['Description'])
        again = self.roundtrip(imported)[0]
        self.assertEqual(imported['Description'], again['Description'])
        self.assertEqual(row['End Date'], '')


if __name__ == '__main__':
    unittest.main()
