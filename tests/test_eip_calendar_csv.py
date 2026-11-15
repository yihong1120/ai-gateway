"""CSV compatibility regressions requiring no database or external service."""
from __future__ import annotations

import csv
import io
import re
import unittest
from urllib.parse import unquote_plus

from examples.official_document_workflow.cli.prepare_calendar_csv import prepare
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_HEADERS
from examples.official_document_workflow.domain.calendar.csv_schema import CALENDAR_TRANSLATION_ROW
from examples.official_document_workflow.infrastructure.eip.calendar_csv import export_eip_csv
from examples.official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
from examples.official_document_workflow.infrastructure.eip.calendar_csv import direct_import_notice
from examples.official_document_workflow.infrastructure.eip.calendar_csv import MIDNIGHT_IMPORT_NOTICE
from examples.official_document_workflow.infrastructure.eip.calendar_csv import MIDNIGHT_FALLBACK_HEADING
from examples.official_document_workflow.infrastructure.eip.netask import csv_minutes


def fixture(**overrides):
    row = {
        **dict.fromkeys(CALENDAR_HEADERS, ''),
        'Subject': '現勘 2K+500，進度 50%，原文 %2B[AI產生]',
        'Start Date': '2026/10/7', 'End Date': '2026/10/7',
        'Start Time': '14:00:00', 'Start Time(24hr)': '14:00',
        'End Time': '15:00:00', 'End Time(24hr)': '15:00',
        'All day event': '假', 'Private': '真', 'Priority': '普通',
        'Description': '原文 2K+500\n比例 50%，字面 %2B',
        **overrides,
    }
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=CALENDAR_HEADERS, quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerow(CALENDAR_TRANSLATION_ROW)
    writer.writerow(row)
    return stream.getvalue().encode('utf-8-sig')


class CsvCompatibilityTests(unittest.TestCase):
    def test_duplicate_generated_work_prefix_does_not_duplicate_download_rows(self):
        source = parse_csv(fixture())[0][0]
        rows = []
        for title, clock, end in (
            ('設施會勘', '10:00', '11:00'), ('排水改善會勘', '10:30', '11:30'), ('用地開口會勘', '11:00', '12:00'),
        ):
            for prefix in ('辦理', ''):
                rows.append({
                    **source, 'Subject': prefix+title+'[AI產生](測試工程)',
                    'Start Time': '', 'End Time': '', 'Start Time(24hr)': clock, 'End Time(24hr)': end,
                    'Custom Field1': '測試字第115001號',
                    'Description': '辦理事項：'+prefix+title+'\n\n公文內容：\n請依原定時段辦理本案會勘。',
                })
        before = [dict(row) for row in rows]
        actual, _ = parse_csv(export_eip_csv(rows), csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(len(actual), 3)
        self.assertEqual([row['Subject'] for row in actual], [row['Subject'] for row in rows[::2]])
        self.assertEqual(rows, before)

    def test_duplicate_filter_keeps_first_row_order_and_different_source_clauses(self):
        source = parse_csv(fixture())[0][0]
        other = {**source, 'Subject': '另一獨立工作'}
        changed = {**source, 'Description': source['Description']+'\n另外一項來源要求。'}
        actual, _ = parse_csv(export_eip_csv([source, other, dict(source), changed]), csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual([row['Subject'] for row in actual], [source['Subject'], other['Subject'], source['Subject']])
        self.assertEqual(actual[-1]['Description'], changed['Description'])

    def test_download_always_has_all_template_columns_even_when_optional_values_are_empty(self):
        download = export_eip_csv([{
            'Subject': '提送報告', 'Start Date': '2026/10/07',
            'End Date': '2026/10/07', 'All day event': '真',
        }])
        records = list(csv.reader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        self.assertEqual(records[0], CALENDAR_HEADERS)
        self.assertEqual(len(records[1]), 21)
        rows, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(rows[0]['Name'], '')
        self.assertEqual(rows[0]['Categories'], '')

    def test_incomplete_or_invalid_rows_never_become_downloads(self):
        valid, _ = parse_csv(fixture())
        for changes in (
            {'Subject': ''}, {'Start Date': ''}, {'End Date': ''},
            {'Start Time': '', 'Start Time(24hr)': ''},
            {'End Date': '2026/10/08', 'End Time': '', 'End Time(24hr)': ''},
            {'End Time': '13:00:00', 'End Time(24hr)': '13:00'},
            {'All day event': ''}, {'Description': 'bad\x00value'},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                export_eip_csv([{**valid[0], **changes}])
        with self.assertRaises(ValueError):
            export_eip_csv([valid[0]] * 101)
        with self.assertRaises(ValueError):
            export_eip_csv([])

    def test_format_version_prevents_guessing_or_double_decoding_literal_percent(self):
        original, _ = parse_csv(fixture())
        download = export_eip_csv(original)
        rows, transport = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(rows[0]['Subject'], original[0]['Subject'])
        self.assertEqual(rows[0]['Description'], original[0]['Description'])
        self.assertEqual(rows[0]['Start Time'], '下午02:00:00')
        # Legacy files containing a literal %2B must not be guessed as encoded.
        legacy, _ = parse_csv(fixture())
        self.assertIn('%2B', legacy[0]['Description'])
        self.assertIn('2K+500', legacy[0]['Description'])
        self.assertEqual(unquote_plus(prepare(fixture())[1].decode('utf-8-sig')), transport.decode('utf-8-sig'))
        with self.assertRaises(ValueError):
            parse_csv(download, csv_format='unrecognized-version')

    def test_download_splits_overnight_and_retains_source_for_existing_import_aliases(self):
        source, _ = parse_csv(fixture())
        row = {
            **source[0], 'Start Time': '23:30:00', 'Start Time(24hr)': '23:30',
            'End Date': '2026/10/08', 'End Time': '00:30:00', 'End Time(24hr)': '00:30',
        }
        download = export_eip_csv([row])
        direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        self.assertEqual(len(direct), 2)
        self.assertEqual(
            [(r['Start Date'], r['End Date'], r['Start Time(24hr)'], r['End Time(24hr)']) for r in direct],
            [('2026/10/07', '2026/10/07', '23:30', '24:00'), ('2026/10/08', '2026/10/08', '', '')],
        )
        self.assertEqual(direct[1]['All day event'], '真')
        self.assertIn('原定時段：2026/10/08 00:00–00:30', direct[1]['Description'])
        imported, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0]['_source_interval']['Start Date'], row['Start Date'])
        self.assertEqual(imported[1]['_source_interval']['End Date'], row['End Date'])
        self.assertEqual(imported[1]['_source_interval']['End Time(24hr)'], '00:30')
        self.assertEqual(direct_import_notice(download), MIDNIGHT_IMPORT_NOTICE)

    def test_noon_uses_the_native_website_clock_convention(self):
        source, _ = parse_csv(fixture(**{
            'Start Time': '12:00:00', 'Start Time(24hr)': '12:00',
            'End Time': '12:30:00', 'End Time(24hr)': '12:30',
        }))
        download = export_eip_csv(source)
        direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        self.assertEqual(direct[0]['Start Time'], '下午00:00:00')
        self.assertEqual(direct[0]['End Time'], '下午00:30:00')
        self.assertEqual(csv_minutes(direct[0]['Start Time']), '720')
        self.assertEqual(csv_minutes(direct[0]['End Time']), '750')
        restored, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(restored[0]['Start Time(24hr)'], '12:00')

    def test_every_minute_exports_to_the_observed_native_website_parser(self):
        # Match the deployed website's csv2mins, including its noon convention.
        def website_minutes(value):
            hour, minute, _ = map(int, re.search(r'\d\d:\d\d:\d\d', value)[0].split(':'))
            total = hour * 60 + minute + (720 if '下午' in value else 0)
            return total - (720 if '上午12:' in value else 0)

        source, _ = parse_csv(fixture())
        for start in range(1440):
            with self.subTest(start_minute=start):
                end = start + 1
                row = {
                    **source[0], 'Start Time': '', 'End Time': '',
                    'Start Time(24hr)': f'{start // 60:02d}:{start % 60:02d}',
                    'End Time(24hr)': f'{end // 60:02d}:{end % 60:02d}',
                }
                download = export_eip_csv([row])
                direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))[0]
                self.assertEqual(len(direct), 21)
                if start == 0:
                    self.assertEqual(direct['All day event'], '真')
                    self.assertEqual(direct['Start Time'], '')
                    self.assertIn('00:00–00:01', direct['Description'])
                else:
                    self.assertEqual(direct['All day event'], '假')
                    self.assertEqual(website_minutes(direct['Start Time']), start)
                    self.assertEqual(website_minutes(direct['End Time']), end)

    def test_midnight_native_reminder_preserves_exact_original_schedule_for_versioned_reads(self):
        source, _ = parse_csv(fixture(**{
            'Start Time': '00:00:00', 'Start Time(24hr)': '00:00',
            'End Time': '01:00:00', 'End Time(24hr)': '01:00',
        }))
        download = export_eip_csv(source)
        direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        self.assertEqual(direct[0]['All day event'], '真')
        self.assertEqual(direct[0]['Start Time'], '')
        self.assertIn('原定時段：2026/10/07 00:00–01:00', direct[0]['Description'])
        restored, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(restored[0]['All day event'], '假')
        self.assertEqual(restored[0]['Start Time(24hr)'], '00:00')
        self.assertEqual(restored[0]['Description'], source[0]['Description'])
        self.assertEqual(export_eip_csv(restored), download)

    def test_document_text_cannot_impersonate_a_generated_midnight_reminder(self):
        description = MIDNIGHT_FALLBACK_HEADING + '\n原定時段：2026/10/07 00:00–01:00\n\n原文內容。'
        download = export_eip_csv([{
            'Subject': '僅有日期', 'Start Date': '2026/10/07', 'End Date': '2026/10/07',
            'All day event': '真', 'Description': description,
        }])
        restored, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(restored[0]['All day event'], '真')
        self.assertEqual(restored[0]['Start Time(24hr)'], '')
        self.assertTrue(restored[0]['Description'].endswith(description))

    def test_segment_roundtrip_preserves_complete_source_text_and_is_idempotent(self):
        rows, _ = parse_csv(fixture())
        source = {
            **rows[0], 'Start Time': '23:30:00', 'Start Time(24hr)': '23:30',
            'End Date': '2026/10/08', 'End Time': '00:30:00', 'End Time(24hr)': '00:30',
            'Description': '原文含換行與尾端空白。\n\n ',
        }
        first = export_eip_csv([source])
        restored, _ = parse_csv(first, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(restored[0]['_source_interval']['Description'], source['Description'])
        self.assertEqual(export_eip_csv(restored), first)

    def test_partial_or_changed_download_segments_cannot_claim_a_source_interval(self):
        rows, _ = parse_csv(fixture())
        source = {
            **rows[0], 'Start Time': '23:30:00', 'Start Time(24hr)': '23:30',
            'End Date': '2026/10/08', 'End Time': '00:30:00', 'End Time(24hr)': '00:30',
        }
        download = export_eip_csv([source])
        direct = list(csv.DictReader(io.StringIO(unquote_plus(download.decode('utf-8-sig')))))
        for invalid in (
            direct[:1], [direct[0], direct[0]],
            [direct[0], {**direct[1], 'Description': direct[1]['Description'].replace('完整區間', '已修改區間')}],
            [direct[0], {**direct[1], 'Custom Field1': '不同來源'}],
        ):
            stream = io.StringIO(newline='')
            writer = csv.DictWriter(stream, fieldnames=CALENDAR_HEADERS)
            writer.writeheader()
            writer.writerows(invalid)
            with self.subTest(rows=invalid), self.assertRaises(ValueError):
                parse_csv(stream.getvalue().encode('utf-8-sig'))

    def test_unpadded_dates_match_netask_without_changing_source(self):
        payload = fixture()
        rows, transport = parse_csv(payload)
        self.assertEqual(rows[0]['Start Date'], '2026/10/07')
        self.assertEqual(rows[0]['End Date'], '2026/10/07')
        imported = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))
        self.assertEqual(imported[0]['Start Date'], rows[0]['Start Date'])
        self.assertIn(b'2026/10/7', payload)

    def test_direct_upload_preserves_text_after_single_eip_decode(self):
        payload = fixture()
        count, direct = prepare(payload)
        expected_rows, normalized = parse_csv(payload)
        self.assertEqual(count, 1)
        self.assertTrue(direct.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(unquote_plus(direct.decode('utf-8-sig')), normalized.decode('utf-8-sig'))
        self.assertEqual(direct_import_notice(direct), '')
        imported = list(csv.DictReader(io.StringIO(unquote_plus(direct.decode('utf-8-sig')))))
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]['Subject'], expected_rows[0]['Subject'])
        self.assertEqual(imported[0]['Description'], expected_rows[0]['Description'])
        self.assertEqual(csv_minutes(imported[0]['Start Time']), '840')
        self.assertEqual(csv_minutes(imported[0]['End Time']), '900')

    def test_default_duration_preserves_midnight_rollover_with_unpadded_dates(self):
        rows, _ = parse_csv(
            fixture(
                **{
                    'Start Time': '23:30:00', 'Start Time(24hr)': '23:30',
                    'End Date': '', 'End Time': '', 'End Time(24hr)': '',
                },
            ), default_duration_minutes=60,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['End Time(24hr)'], '24:00')
        self.assertEqual(rows[1]['Start Date'], '2026/10/08')
        self.assertEqual(rows[1]['End Time(24hr)'], '00:30')


if __name__ == '__main__':
    unittest.main()
