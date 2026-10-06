"""CSV compatibility regressions requiring no database or external service."""
from __future__ import annotations

import csv
import io
import unittest
from urllib.parse import unquote_plus

from examples.prepare_eip_calendar_csv import prepare
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_HEADERS
from official_document_workflow.domain.calendar.csv_schema import CALENDAR_TRANSLATION_ROW
from official_document_workflow.infrastructure.eip.calendar_csv import export_eip_csv
from official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
from official_document_workflow.infrastructure.eip.netask import csv_minutes


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

    def test_download_retains_overnight_source_for_existing_import_aliases(self):
        source, _ = parse_csv(fixture())
        row = {
            **source[0], 'Start Time': '23:30:00', 'Start Time(24hr)': '23:30',
            'End Date': '2026/10/08', 'End Time': '00:30:00', 'End Time(24hr)': '00:30',
        }
        download = export_eip_csv([row])
        imported, _ = parse_csv(download, csv_format=CALENDAR_CSV_FORMAT)
        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0]['_source_interval']['Start Date'], row['Start Date'])
        self.assertEqual(imported[1]['_source_interval']['End Date'], row['End Date'])
        self.assertEqual(imported[1]['_source_interval']['End Time(24hr)'], '00:30')

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
