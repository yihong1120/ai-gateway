from __future__ import annotations

import copy
import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from official_document_workflow.infrastructure.eip.calendar_repair import fingerprint
from official_document_workflow.infrastructure.eip.calendar_repair import repair_parameters
from official_document_workflow.infrastructure.eip.calendar_sync import write_events
from official_document_workflow.infrastructure.eip.calendar_verification import validate_wire
from official_document_workflow.infrastructure.eip.calendar_verification import verify_detail
from official_document_workflow.infrastructure.eip.netask import js_escape
from official_document_workflow.infrastructure.eip.netask import Netask
from official_document_workflow.infrastructure.eip.netask import parse_rpc
from official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase

EVENT = '2026-09-07 21:24:14.709;791504605'


def fixture():
    fields = ['']*32
    for index, value in {
        0: js_escape('測試會議'), 1: '2026-09-18', 2: '590', 3: '2026-09-18', 4: '650',
        5: '0', 14: '1', 19: js_escape('說明'), 20: '1', 29: 'Ru1709', 31: 'Ru1709',
    }.items():
        fields[index] = value
    row = ['']*43
    for index, value in {
        0: EVENT, 2: '1709', 8: fields[0], 9: 'new Date(2026, 8, 18, 0, 0, 0)',
        10: '0', 11: '590', 12: '650', 13: '1', 14: fields[19], 15: 'Ru1709', 17: '0', 22: '0', 23: '0',
    }.items():
        row[index] = value
    row[37:42] = ['文號', '日期', '收文者', '承辦', '單位']
    listing = ['']*21
    for index, value in {0: '1709', 2: EVENT, 6: fields[0], 8: '0', 9: '590', 10: '650'}.items():
        listing[index] = value
    return ','.join(fields), [row, [0]], listing


class VerificationTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def test_all_day_edit_omits_time_fields_like_official_form(self):
        line, detail, _ = fixture()
        fields = line.split(',')
        fields[5] = '1'
        fields[2] = fields[4] = ''
        detail[0][10] = '1'
        detail[0][11] = detail[0][12] = ''
        params = repair_parameters(detail, ','.join(fields), '1709', EVENT, fingerprint(detail))
        self.assertEqual(params['all_day'], '1')
        self.assertNotIn('start_time', params)
        self.assertNotIn('end_time', params)

    def test_browser_breaking_raw_newline_rejected_by_strict_calendar_parser(self):
        text = 'var data=[[0,""],["Ru1709\n"]];'
        self.assertEqual(parse_rpc(text)[1], ['Ru1709\n'])
        with self.assertRaises(ValueError):
            parse_rpc(text, strict=True)
        self.assertEqual(parse_rpc('var data='+json.dumps([[0, ''], ['Ru1709']])+';', strict=True)[1], ['Ru1709'])

    async def test_commit_requires_detail_verification_and_records_receipt(self):
        line, detail, listing = fixture()
        n = Netask()
        n.user_id = '1709'
        n.rpc = AsyncMock(side_effect=[[[0, '']], [[0, ''], listing], [[0, ''], *detail]])
        try:
            with tempfile.TemporaryDirectory() as _:
                journal = Journal()
                self.assertEqual(await write_events(n, [{'key': 'new', 'aliases': ['old'], 'line': line}], journal), {'created': 1, 'skipped': 0})
                self.assertEqual(journal.status('old'), 'done')
                with journal.connect() as db:
                    self.assertEqual(
                        db.execute(
                            'SELECT event_id FROM event_receipts WHERE key=%s',
                            ('new',),
                        ).fetchone()[0], EVENT,
                    )
            self.assertTrue(n.rpc.call_args.kwargs['strict_json'])
        finally:
            await n.close()

    async def test_success_response_but_bad_detail_stays_unknown_no_replay(self):
        line, detail, listing = fixture()
        detail[0][15] = 'Ru1709\n'
        n = Netask()
        n.user_id = '1709'
        n.rpc = AsyncMock(side_effect=[[[0, '']], [[0, ''], listing], [[0, ''], *detail]])
        try:
            with tempfile.TemporaryDirectory() as _:
                journal = Journal()
                events = [{'key': 'k', 'line': line}]
                with self.assertRaises(ValueError):
                    await write_events(n, events, journal)
                self.assertEqual(journal.status('k'), 'unknown')
                with self.assertRaises(ValueError):
                    await write_events(n, events, journal)
                self.assertEqual(n.rpc.await_count, 3)
        finally:
            await n.close()

    def test_wire_and_detail_reject_cross_owner_sharing_and_changed_content(self):
        line, detail, _ = fixture()
        for value in (line+'\n', line.replace('Ru1709', 'Ru999')):
            with self.assertRaises(ValueError):
                validate_wire(value, '1709')
        for index, value in ((2, '999'), (4, '77'), (14, 'different'), (15, 'Ru1709,Ru999'), (12, '720')):
            d = copy.deepcopy(detail)
            d[0][index] = value
            with self.assertRaises(ValueError):
                verify_detail(d, line.split(','), '1709', EVENT)

    def test_explicit_repair_preserves_custom_fields_and_never_sends_mail(self):
        line, detail, _ = fixture()
        detail[0][15] = 'Ru1709\n'
        detail[0][12] = '590'
        params = repair_parameters(detail, line, '1709', EVENT, fingerprint(detail))
        self.assertEqual(params['event_id'], EVENT)
        self.assertEqual(params['end_time'], '650')
        self.assertEqual(params['pub'], 'Ru1709')
        # Edit preserves persisted custom fields verbatim; re-escaping raw
        # Chinese expands long department names and causes the live EIP 500.
        self.assertEqual(params['user_def_value[0]'], '文號')
        encoded = copy.deepcopy(detail)
        encoded[0][37] = js_escape('文號')
        self.assertEqual(
            repair_parameters(encoded, line, '1709', EVENT, fingerprint(encoded))
            ['user_def_value[0]'], js_escape('文號'),
        )
        self.assertEqual((params['isMailtoShared'], params['mto'], params['remind_who']), ('', '', '0000'))
        with self.assertRaises(ValueError):
            repair_parameters(detail, line, '1709', EVENT, 'stale')
        for index, value in ((2, 'other'), (15, 'Ru1709,Ru99'), (22, '1'), (11, '580'), (9, 'new Date(2026, 8, 19, 0, 0, 0)'), (31, '15'), (32, '1000')):
            d = copy.deepcopy(detail)
            d[0][index] = value
            with self.assertRaises(ValueError):
                repair_parameters(d, line, '1709', EVENT, fingerprint(d))
        with self.assertRaises(ValueError):
            repair_parameters(
                [detail[0], [1], ['attachment']], line, '1709',
                EVENT, fingerprint([detail[0], [1], ['attachment']]),
            )
