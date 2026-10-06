"""Offline EIP contract, authorization, preview, and replay-safety tests."""
from __future__ import annotations

import asyncio
import csv
import importlib
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request

from official_document_workflow.application import integration
from official_document_workflow.domain.calendar import csv_schema as SCHEMA
from official_document_workflow.infrastructure.eip import calendar_csv
from official_document_workflow.infrastructure.eip.netask import import_line
from official_document_workflow.infrastructure.eip.netask import js_escape
from official_document_workflow.infrastructure.eip.netask import Netask
from official_document_workflow.infrastructure.eip.netask import parse_rpc
from official_document_workflow.infrastructure.storage.automation_store import (
    AutomationStore,
)
from official_document_workflow.infrastructure.storage.state import Journal
from official_document_workflow.infrastructure.storage.state import Link
from official_document_workflow.infrastructure.storage.state import State
from tests.eip_postgres import EIPPostgresTestCase

ROOT = Path(__file__).resolve().parents[1]


class RetryClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_temporary_http_failures_are_retryable_and_no_transport_replay(
        self,
    ):
        for status in (408, 429, 500, 502, 503, 504, 401, 403, 404):
            calls = []

            def handler(req):
                calls.append(req)
                return httpx.Response(status, text='private upstream body')

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
            ) as client:
                n = Netask(client)
                expected = (
                    ConnectionError
                    if status == 401
                    else (
                        TimeoutError
                        if status in (408, 429, 500, 502, 503, 504)
                        else ValueError
                    )
                )
                with self.assertRaises(expected) as raised:
                    await n.request('GET', '/tw/cm/index_cm.htm')
                if status in (403, 404):
                    self.assertNotIsInstance(raised.exception, TimeoutError)
                self.assertNotIn('private upstream', str(raised.exception))
                self.assertEqual(len(calls), 1)


def row():
    return {
        **dict.fromkeys(SCHEMA.CALENDAR_HEADERS, ''),
        'Name': '承辦人',
        'Subject': '測試公文',
        'Start Date': '2026/09/10',
        'End Date': '2026/09/10',
        'All day event': '真',
        'Private': '假',
        'Description': '說明第一行\n第二行',
        'Priority': '普通',
        'Completed': '0',
    }


def csv_bytes(rows, translation=True):
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream, fieldnames=SCHEMA.CALENDAR_HEADERS, quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    if translation:
        writer.writerow(SCHEMA.CALENDAR_TRANSLATION_ROW)
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def remote(r=None):
    r = r or row()
    result = [''] * 29
    for key, idx in [
        ('Subject', 0),
        ('Start Date', 1),
        ('Start Time', 2),
        ('End Date', 3),
        ('End Time', 4),
        ('All day event', 5),
        ('Private', 14),
        ('Description', 19),
        ('Priority', 20),
        ('Categories', 21),
        *[(f"Custom Field{i}", 22 + i) for i in range(1, 6)],
    ]:
        result[idx] = r[key].replace('\n', '%0D%0A')
    result[28] = '0'
    return result


class ContractTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def test_uppercase_description_preview_writes_original_case(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
        )
        from official_document_workflow.infrastructure.eip.calendar_read import unescape

        source = {
            **row(),
            'Description': r'說明：\\underline{\\text{115年9月9日前提送}} fileAb.dwg 0K+100'
            + '\n第二行',
        }
        staged = remote(source)
        staged[19] = staged[19].upper()
        original_preview = staged[19]
        with tempfile.TemporaryDirectory() as _:
            events = verified_events([source], [staged], '1709', Journal())
        self.assertEqual(
            unescape(events[0]['line'].split(',')[19]).replace('\r\n', '\n'),
            source['Description'],
        )
        self.assertEqual(staged[19], original_preview)

    def test_exact_preview_preserves_existing_wire_and_dedup_key(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
        )

        source = {**row(), 'Description': '既有說明\n第二行 fileAb.dwg'}
        staged = remote(source)
        old_line = import_line(staged, '1709')
        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            key = journal.key('1709', {'import': old_line})
            journal.reserve_event(key)
            journal.done(key)
            event = verified_events([source], [staged], '1709', journal)[0]
            self.assertEqual(event['line'], old_line)
            self.assertEqual(event['key'], key)
            self.assertEqual(journal.status(event['key']), 'done')

    def test_uppercase_preview_does_not_accept_dates_text_or_unicode_changes(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
        )

        source = {**row(), 'Description': 'fileAb.dwg 115年9月9日 é 原文'}
        for changed in (
            'FILEAB.DWG 115年9月8日 é 原文',
            'FILEAB.DWG 115年9月9日 é 改寫',
            'FILEAB.DWG 115年9月9日 É 原文',
            'FILEAB.DWG 115年9月9日 é 原文 EXTRA',
        ):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as _:
                staged = remote(source)
                staged[19] = changed
                with self.assertRaises(ValueError):
                    verified_events([source], [staged], '1709', Journal())

    async def test_csv_transport_preserves_literal_chainage_and_percent_sequences(self):
        from urllib.parse import unquote_plus

        raw = csv_bytes(
            [
                {
                    **row(),
                    'Subject': 'K+100 %2B',
                    'Description': '0K+000～0K+100 100% %0A',
                },
            ],
        )
        n = Netask()
        n.rpc = AsyncMock(return_value=[[0, '']])
        try:
            await n.stage(raw)
            transport = n.rpc.await_args.kwargs['files']['importfile'][1]
            self.assertEqual(
                unquote_plus(transport.decode('utf-8-sig')), raw.decode('utf-8-sig'),
            )
            self.assertIn(b'0K%2B000', transport)
            self.assertIn(b'%252B', transport)
        finally:
            await n.close()

    def test_calendar_wire_preserves_plus_in_description(self):
        from official_document_workflow.infrastructure.eip.calendar_read import unescape

        source = {**row(), 'Description': '0K+000～0K+100 100% %2B'}
        line = import_line(remote(source), '1709')
        self.assertIn('%2B', line.split(',')[19])
        self.assertEqual(unescape(line.split(',')[19]), source['Description'])

    async def test_plus_escape_fix_cannot_bypass_unknown_legacy_write(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
            write_events,
        )

        source = {**row(), 'Description': '0K+100'}
        line = import_line(remote(source), '1709')
        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            old_key = journal.key('1709', {'import': line.replace('%2B', '+')})
            journal.reserve_event(old_key)
            events = verified_events([source], [remote(source)], '1709', journal)
            self.assertIn(old_key, events[0]['aliases'])
            adapter = types.SimpleNamespace(commit=AsyncMock(return_value=None))
            with self.assertRaises(ValueError):
                await write_events(adapter, events, journal)
            adapter.commit.assert_not_awaited()

    def test_import_title_encoding_limit_preserves_full_source_and_download(self):
        original = row()
        original['Subject'] = '工程稽核缺失改善與後續提報' * 15
        payload = csv_bytes([original])
        before = bytes(payload)
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, transport = calendar_csv.parse_csv(payload)
        self.assertEqual(payload, before)
        self.assertLessEqual(len(rows[0]['Subject'].encode('utf-8')), 200)
        self.assertTrue(rows[0]['Subject'].endswith('…'))
        self.assertIn(original['Subject'], rows[0]['Description'])
        self.assertTrue(rows[0]['Description'].endswith(original['Description']))
        self.assertEqual(rows[0]['Start Date'], original['Start Date'])
        self.assertEqual(calendar_csv.fit_calendar_subject(rows[0]), rows[0])
        imported = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))[0]
        self.assertEqual(imported['Description'], rows[0]['Description'])

    def test_title_boundary_and_ordinary_title_do_not_change(self):
        for title in ('測試公文', 'a' * 200, '😀' * 50, '測' * 66):
            source = {**row(), 'Subject': title}
            self.assertEqual(calendar_csv.fit_calendar_subject(source), source)
        source = {**row(), 'Subject': 'a' * 201}
        self.assertNotEqual(
            calendar_csv.fit_calendar_subject(source)['Subject'], source['Subject'],
        )

    def test_parser_never_evaluates_javascript(self):
        self.assertEqual(
            parse_rpc('<script>var data=[[0,""],["a\x01b"]];parent.cb()</script>')[1][
                0
            ],
            'a\x01b',
        )
        for value in ['var data=alert(1)', 'var data=[[999]];', '<html>login</html>']:
            with self.assertRaises((ValueError, ValueError)):
                parse_rpc(value)

    def test_js_legacy_escape(self):
        self.assertEqual(js_escape('A中,\n😀'), 'A%u4E2D%2C%0A%uD83D%uDE00')

    async def test_document_detail_returns_form_content_and_supported_attachments(self):
        header = [''] * 55
        header[13] = (
            'Text7=%u5DE5%u5340\x01Select1=115\x01Select2=9\x01Select3=10\x01'
            'Text1=%u767C%u6587%u55AE%u4F4D\x01Select12=115\x01Select13=9\x01'
            'Select14=8\x01Text6=A123\x01TextArea1=%u4E3B%u65E8%0A%u5167%u6587'
        )
        header[17] = '2'
        pdf = ['', '', '公文.pdf', 'http://eip.jiedar.com.tw/fo/a.pdf', '', '', '1']
        image = ['', '', '略圖.png', 'http://eip.jiedar.com.tw/fo/a.png', '', '', '1']
        adapter = Netask()
        adapter.rpc = AsyncMock(return_value=[[0, ''], header, pdf, image])
        detail = await adapter.document_detail('1', 'inbox')
        await adapter.close()
        self.assertEqual(
            detail['fields'],
            [
                {'label': '工區', 'value': '工區'},
                {'label': '收文日期', 'value': '115年9月10日'},
                {'label': '發文單位', 'value': '發文單位'},
                {'label': '發文日期', 'value': '115年9月8日'},
                {'label': '發文字號', 'value': 'A123'},
                {'label': '本文主旨', 'value': '主旨\n內文'},
            ],
        )
        self.assertEqual(detail['attachments'], [{'name': '公文.pdf', 'url': pdf[3]}])

    def test_import_owner_private_no_company_share(self):
        line = import_line(remote(), '1709')
        cols = line.rstrip('\n').split(',')
        self.assertEqual(cols[14], '1')
        self.assertEqual(cols[6], '0')
        self.assertEqual(cols[-3:], ['Ru1709', '', 'Ru1709'])
        self.assertNotIn('\n', line)
        self.assertNotIn('\r', line)
        self.assertNotIn('承辦人', line)
        self.assertIn('%0D%0A', line)

    def test_timed_import_requires_times(self):
        r = remote()
        r[5] = '假'
        with self.assertRaises(ValueError):
            import_line(r, '1')
        r[2], r[4] = '上午09:00:00', '下午12:00:00'
        cols = import_line(r, '1').split(',')
        self.assertEqual((cols[2], cols[4]), ('540', '720'))

    def test_unsafe_custom_field_fails_closed(self):
        r = remote()
        r[23] = 'A,B'
        with self.assertRaises(ValueError):
            import_line(r, '1')

    async def test_no_arbitrary_host_or_non_pdf(self):
        called = []

        def handler(req):
            called.append(req)
            return httpx.Response(200, content=b'not PDF')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            n = Netask(client)
            for url in [
                'http://evil.example/fo/x.pdf',
                'http://eip.jiedar.com.tw/admin',
                'http://eip.jiedar.com.tw/fo/x.pdf',
            ]:
                with self.assertRaises(ValueError):
                    await n.download(url)
            self.assertEqual(len(called), 1)

    async def test_request_size_bound(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b'12345'),
            ),
        ) as client:
            with self.assertRaises(ValueError):
                await Netask(client).request('GET', '/login', limit=4)

    async def test_stage_only_parser_never_update(self):
        calls = []

        def handler(req):
            calls.append(req.content)
            return httpx.Response(
                200,
                text='var data='
                + json.dumps([[0, ''], remote(), [''] * 28 + ['0']])
                + ';',
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await Netask(client).stage(csv_bytes([row()]))
        self.assertEqual(len(result), 1)
        self.assertIn(b'CM_Import', calls[0])
        self.assertNotIn(b'import_ready', calls[0])

    async def test_empty_folder_date_filter_sentinel(self):
        sentinel = [''] * 21
        sentinel[16:18] = ['2026-08-01', '2026-09-05']
        n = Netask()
        n.rpc = AsyncMock(return_value=[[0, '', 'Page=0TPage=0Count=0'], sentinel])
        try:
            self.assertEqual(
                await n.documents('notify', '2026-08-01', '2026-09-05'), [],
            )
        finally:
            await n.close()

    def test_csv_schema_translation_and_24hr_conversion(self):
        r = row()
        r.update(
            {
                'All day event': '假',
                'Start Time(24hr)': '09:00',
                'End Time(24hr)': '12:00',
            },
        )
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, payload = calendar_csv.parse_csv(csv_bytes([r]))
        parsed = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['Start Time'], '上午09:00:00')
        self.assertEqual(parsed[0]['End Time'], '下午12:00:00')
        self.assertEqual(rows, [r])
        self.assertEqual(rows[0]['Start Time'], '')

    def test_csv_rejects_missing_dates_conflicting_times(self):
        for changes in [
            {'Start Date': ''},
            {'End Date': '2026/09/09'},
            {'Start Time(24hr)': '09:00', 'Start Time': '下午09:00:00'},
        ]:
            with patch.object(integration, 'pipe_module', return_value=SCHEMA):
                with self.assertRaises((ValueError, ValueError)):
                    calendar_csv.parse_csv(csv_bytes([{**row(), **changes}]))

    async def test_automatic_start_only_reserves_hour_without_changing_source(self):
        r = {
            **row(),
            'All day event': '假',
            'Start Time': '09:50:00',
            'Start Time(24hr)': '09:50',
            'End Date': '',
            'End Time': '',
            'End Time(24hr)': '',
        }
        original = csv_bytes([r])
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            with self.assertRaises((ValueError, ValueError)):
                calendar_csv.parse_csv(original)
            rows, transport = calendar_csv.parse_csv(
                original, default_duration_minutes=60,
            )
        self.assertEqual(rows[0]['End Date'], r['Start Date'])
        self.assertEqual(rows[0]['End Time(24hr)'], '10:50')
        self.assertIn(calendar_csv.duration_notice(60), rows[0]['Description'])
        self.assertEqual(original, csv_bytes([r]))
        parsed = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))[0]
        self.assertEqual(parsed['Start Time'], '上午09:50:00')
        self.assertEqual(parsed['End Time'], '上午10:50:00')
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
            write_events,
        )

        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            events = verified_events(rows, [remote(parsed)], '12', journal)
            adapter = types.SimpleNamespace(commit=AsyncMock(return_value=None))
            self.assertEqual(
                await write_events(adapter, events, journal),
                {'created': 1, 'skipped': 0},
            )
            self.assertEqual(
                await write_events(adapter, events, Journal()),
                {'created': 0, 'skipped': 1},
            )
            adapter.commit.assert_awaited_once()

    def test_automatic_point_does_not_guess_other_missing_or_conflicting_values(self):
        base = {
            **row(),
            'All day event': '假',
            'Start Time': '09:50:00',
            'Start Time(24hr)': '09:50',
            'End Date': '',
            'End Time': '',
            'End Time(24hr)': '',
        }
        for changes in (
            {'Start Date': ''},
            {'Start Time': '', 'Start Time(24hr)': ''},
            {'End Date': '2026/09/11'},
            {'Start Time': '09:50:30'},
            {'Start Time': '下午09:50:00'},
            {'All day event': '真'},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(integration, 'pipe_module', return_value=SCHEMA),
            ):
                with self.assertRaises((ValueError, ValueError)):
                    calendar_csv.parse_csv(
                        csv_bytes([{**base, **changes}]), default_duration_minutes=60,
                    )

    def test_automatic_point_accepts_same_day_end_date_without_an_end_clock(self):
        base = {
            **row(),
            'All day event': '假',
            'Start Time': '09:50:00',
            'Start Time(24hr)': '09:50',
            'End Date': '2026/09/10',
            'End Time': '',
            'End Time(24hr)': '',
        }
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, _ = calendar_csv.parse_csv(
                csv_bytes([base]), default_duration_minutes=60,
            )
        self.assertEqual(rows[0]['End Date'], '2026/09/10')
        self.assertEqual(rows[0]['End Time(24hr)'], '10:50')

    def test_default_duration_midnight_rollover_config_and_explicit_end(self):
        base = {
            **row(),
            'All day event': '假',
            'Start Time': '23:50:00',
            'Start Time(24hr)': '23:50',
            'End Date': '',
            'End Time': '',
            'End Time(24hr)': '',
        }
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, _ = calendar_csv.parse_csv(
                csv_bytes([base]), default_duration_minutes=120,
            )
            self.assertEqual(
                [
                    (r['Start Date'], r['Start Time(24hr)'], r['End Time(24hr)'])
                    for r in rows
                ],
                [('2026/09/10', '23:50', '24:00'), ('2026/09/11', '00:00', '01:50')],
            )
            explicit = {
                **base,
                'End Date': '2026/09/11',
                'End Time': '03:00:00',
                'End Time(24hr)': '03:00',
            }
            explicit_rows = calendar_csv.parse_csv(
                csv_bytes([explicit]), default_duration_minutes=60,
            )[0]
            self.assertEqual(explicit_rows[-1]['End Time(24hr)'], '03:00')
            self.assertNotIn('系統預留', explicit_rows[0]['Description'])
            for invalid in (True, 0, 30, 180, '60'):
                with self.assertRaises((ValueError, ValueError)):
                    calendar_csv.parse_csv(
                        csv_bytes([base]), default_duration_minutes=invalid,
                    )
        import os

        with patch.dict(os.environ, {'EIP_CALENDAR_DEFAULT_DURATION_MINUTES': '120'}):
            self.assertEqual(calendar_csv.automatic_duration_minutes(), 120)
        with patch.dict(os.environ, {'EIP_CALENDAR_DEFAULT_DURATION_MINUTES': 'bad'}):
            with self.assertRaises((ValueError, ValueError)):
                calendar_csv.automatic_duration_minutes()

    async def test_overnight_segments_preserve_minutes_and_deduplicate(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
            write_events,
        )

        original = {
            **row(),
            'All day event': '假',
            'Start Time(24hr)': '21:00',
            'End Date': '2026/09/11',
            'End Time(24hr)': '06:00',
        }
        source = csv_bytes([original])
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, transport = calendar_csv.parse_csv(source)
        parsed = list(csv.DictReader(io.StringIO(transport.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(parsed[0]['End Time'], '上午24:00:00')
        self.assertNotIn('_source_interval', parsed[0])
        self.assertEqual(csv_bytes([original]), source)
        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            events = verified_events(rows, [remote(r) for r in parsed], '12', journal)
            self.assertEqual(
                [e['line'].split(',')[2:5:2] for e in events],
                [['1260', '1440'], ['0', '360']],
            )
            adapter = types.SimpleNamespace(commit=AsyncMock(return_value=None))
            self.assertEqual(
                await write_events(adapter, events, journal),
                {'created': 2, 'skipped': 0},
            )
            self.assertEqual(
                await write_events(adapter, events, journal),
                {'created': 0, 'skipped': 2},
            )
            self.assertEqual(adapter.commit.await_count, 2)
            # Old full-interval writes cannot be bypassed by the new segments.
            prior = {
                **original,
                'Start Time': '下午09:00:00',
                'End Time': '上午06:00:00',
            }
            legacy = journal.key(
                '12', {'import': import_line(remote(prior), '12') + '\n'},
            )
            journal.reserve(legacy)
            with self.assertRaisesRegex(ValueError, '既有跨日'):
                verified_events(rows, [remote(r) for r in parsed], '12', journal)
            journal.done(legacy)
            with self.assertRaisesRegex(ValueError, '既有跨日'):
                verified_events(rows, [remote(r) for r in parsed], '12', journal)

    def test_midnight_boundaries_and_segment_capacity(self):
        from official_document_workflow.infrastructure.eip.netask import csv_minutes

        self.assertEqual(csv_minutes('上午24:00:00', allow_end_of_day=True), '1440')
        for clock in ('上午24:01:00', '下午24:00:00', '上午24:00:01'):
            with self.assertRaises((ValueError, ValueError)):
                csv_minutes(clock, allow_end_of_day=True)
        with self.assertRaises((ValueError, ValueError)):
            csv_minutes('上午24:00:00')
        base = {
            **row(),
            'All day event': '假',
            'Start Time(24hr)': '21:00',
            'End Date': '2026/09/11',
            'End Time(24hr)': '00:00',
        }
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, _ = calendar_csv.parse_csv(csv_bytes([base]))
            self.assertEqual(len(rows), 1)  # No phantom midnight event.
            self.assertEqual(rows[0]['End Time(24hr)'], '24:00')
            for changes in (
                {'Start Time(24hr)': '24:00'},
                {'End Date': '2026/09/10', 'End Time(24hr)': '20:00'},
                {'End Date': '2027/09/11'},
            ):
                with self.assertRaises((ValueError, ValueError)):
                    calendar_csv.parse_csv(csv_bytes([{**base, **changes}]))

    async def test_legacy_wire_receipt_still_blocks_duplicate_after_fix(self):
        from official_document_workflow.infrastructure.eip.calendar_sync import (
            verified_events,
            write_events,
        )

        with tempfile.TemporaryDirectory() as _:
            journal = Journal()
            legacy = journal.key('12', {'import': import_line(remote(), '12') + '\n'})
            journal.reserve(legacy)
            events = verified_events([row()], [remote()], '12', journal)
            adapter = types.SimpleNamespace(commit=AsyncMock(return_value=None))
            with self.assertRaises(ValueError):
                await write_events(adapter, events, journal)
            journal.done(legacy)
            self.assertEqual(
                await write_events(adapter, events, journal),
                {'created': 0, 'skipped': 1},
            )
            adapter.commit.assert_not_awaited()

    def test_producer_display_time_without_am_pm_is_losslessly_converted(self):
        r = {
            **row(),
            'All day event': '假',
            'Start Time': '13:50:00',
            'Start Time(24hr)': '13:50',
            'End Time': '14:50:00',
            'End Time(24hr)': '14:50',
        }
        with patch.object(integration, 'pipe_module', return_value=SCHEMA):
            rows, payload = calendar_csv.parse_csv(csv_bytes([r]))
        parsed = list(csv.DictReader(io.StringIO(payload.decode('utf-8-sig'))))[0]
        self.assertEqual(parsed['Start Time'], '下午01:50:00')
        self.assertEqual(rows, [r])

    def test_journal_survives_restart_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as _:
            a = Journal()
            key = a.key('12', row())
            self.assertTrue(a.reserve(key))
            b = Journal()
            self.assertEqual(b.status(key), 'unknown')
            self.assertFalse(b.reserve(key))
            b.done(key)
            self.assertEqual(a.status(key), 'done')
            self.assertNotEqual(key, a.key('13', row()))

    async def test_expiry_and_user_isolation(self):
        state = State()
        adapter = types.SimpleNamespace(close=AsyncMock())
        state.links['a'] = Link(adapter, 'account', expires=time.time() - 1)
        with self.assertRaises(ValueError):
            state.link('a')
        await state.sweep()
        adapter.close.assert_awaited_once()
        with self.assertRaises(ValueError):
            state.owned({'x': {'owner': 'a', 'expires': time.time() + 10}}, 'x', 'b')


async def verified(request: Request):
    owner = request.headers.get('test-owner')
    if not owner:
        raise HTTPException(401)
    return types.SimpleNamespace(
        id=owner, role=request.headers.get('test-role', 'user'),
    )


class RouterTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        auth = types.ModuleType('open_webui.utils.auth')
        auth.get_verified_user = verified
        auth.get_current_user = verified
        with patch.dict(sys.modules, {'open_webui.utils.auth': auth}):
            self.module = importlib.import_module(
                'official_document_workflow.api.routes',
            )
        self.state = State()
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal()
        self.auto = AutomationStore(Path(self.tmp.name) / 'keys/key')
        self.patches = [
            patch.object(self.module, 'STATE', self.state),
            patch.object(self.module, 'JOURNAL', self.journal),
            patch.object(self.module.automation, 'store', return_value=self.auto),
        ]
        for p in self.patches:
            p.start()
        self.adapter = types.SimpleNamespace(
            user_id='1709',
            stage=AsyncMock(return_value=[remote()]),
            commit=AsyncMock(return_value=None),
            close=AsyncMock(),
        )
        self.state.links['alice'] = Link(self.adapter, 'account-a')
        self.app = FastAPI()
        self.app.include_router(self.module.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url='https://webui.test',
            headers={
                'test-owner': 'alice',
                'origin': 'https://webui.test',
                'x-eip-action': '1',
            },
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def preview(self):
        with patch.object(
            integration,
            'read_csv',
            AsyncMock(return_value=([row()], csv_bytes([row()]))),
        ):
            r = await self.client.post(
                '/api/v1/eip/preview', json={'file_id': 'owned-csv'},
            )
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['token']

    async def test_history_pagination_reaches_old_records_and_is_owner_scoped(self):
        self.auto.discover(
            'alice',
            '1709',
            [
                {'id': str(i), 'subject': f"公文{i}", 'folder': 'inbox'}
                for i in range(105)
            ],
        )
        self.auto.discover(
            'bob',
            '18',
            [{'id': 'private', 'subject': 'Bob private', 'folder': 'inbox'}],
        )
        with self.auto.db() as db:
            db.execute('UPDATE tasks SET updated=100')
        seen = set()
        for page in range(1, 22):
            response = await self.client.get(
                '/api/v1/eip/status', params={'automatic_page': page},
            )
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            self.assertEqual(
                value['automatic_pagination'],
                {'page': page, 'page_size': 5, 'pages': 21, 'total': 105},
            )
            ids = {r['id'] for r in value['automatic_results']}
            self.assertEqual(len(ids), 5)
            self.assertFalse(seen & ids)
            seen.update(ids)
            self.assertTrue(
                all(r['subject'].startswith('公文') for r in value['automatic_results']),
            )
        self.assertEqual(len(seen), 105)
        response = await self.client.get(
            '/api/v1/eip/status?automatic_page=999&automatic_page_size=20',
        )
        self.assertEqual(
            response.json()['automatic_pagination'],
            {'page': 6, 'page_size': 20, 'pages': 6, 'total': 105},
        )
        self.assertEqual(len(response.json()['automatic_results']), 5)
        response = await self.client.get(
            '/api/v1/eip/status', headers={'test-owner': 'bob'},
        )
        self.assertEqual(response.json()['automatic_pagination']['total'], 1)
        self.assertEqual(
            response.json()['automatic_results'][0]['subject'], 'Bob private',
        )

    async def test_history_empty_and_invalid_pagination(self):
        value = (await self.client.get('/api/v1/eip/status?automatic_page=10')).json()
        self.assertEqual(value['automatic_results'], [])
        self.assertEqual(
            value['automatic_pagination'],
            {'page': 1, 'page_size': 5, 'pages': 1, 'total': 0},
        )
        for query in (
            'automatic_page=0',
            'automatic_page=-1',
            'automatic_page=abc',
            'automatic_page_size=0',
            'automatic_page_size=21',
            'automatic_page_size=1.5',
        ):
            response = await self.client.get('/api/v1/eip/status?' + query)
            self.assertEqual(response.status_code, 422)

    async def test_history_timestamp_provenance(self):
        doc = {
            'id': '10',
            'folder': 'inbox',
            'subject': '公文',
            'date': '2026-09-03 16:26:59.497',
        }
        self.auto.discover('alice', '1709', [doc])
        result = (await self.client.get('/api/v1/eip/status')).json()[
            'automatic_results'
        ][0]
        self.assertEqual(result['eip_recorded_at'], '2026-09-03T16:26:59+08:00')
        self.assertIsNotNone(result['first_detected_at'])
        self.assertIsNone(result['last_processed_at'])
        self.assertNotIn('updated', result)
        task = self.auto.tasks('alice')[0]
        self.auto.save_task(task, 'done', task['payload'])
        later = (await self.client.get('/api/v1/eip/status')).json()[
            'automatic_results'
        ][0]
        self.assertEqual(result['first_detected_at'], later['first_detected_at'])
        self.assertIsNotNone(later['last_processed_at'])

    async def test_dashboard_requires_login(self):
        csv_id = '7030cb9f-a897-4ee4-b79f-bce51a9d652e'
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='https://webui.test',
        ) as guest:
            for path in ['/eip', '/eip/']:
                r = await guest.get(path + '?csv=' + csv_id)
                self.assertEqual(r.status_code, 303)
                self.assertEqual(
                    r.headers['location'],
                    '/auth?redirect=%2Feip%3Fcsv%3D' + csv_id,
                )
                self.assertNotIn('EIP 密碼', r.text)
            r = await guest.get('/api/v1/eip/status')
            self.assertEqual(r.status_code, 401)
            self.assertNotIn('location', r.headers)

    async def test_dashboard_cannot_redirect_to_external_input(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url='https://webui.test',
        ) as guest:
            r = await guest.get(
                '/eip',
                params={
                    'csv': 'https://evil.test',
                    'redirect': '//evil.test',
                    'next': 'https://evil.test',
                },
            )
            self.assertEqual(r.status_code, 303)
            self.assertEqual(r.headers['location'], '/auth?redirect=%2Feip')

    async def test_dashboard_logged_in_and_pending_role(self):
        r = await self.client.get('/eip')
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/eip/assets/app.css"', r.text)
        self.assertIn('src="/eip/assets/app.js"', r.text)
        self.assertIn('href="/connections#eip"', r.text)
        self.assertNotIn('id="password"', r.text)
        self.assertIn("frame-ancestors 'none'", r.headers['content-security-policy'])
        r = await self.client.get('/eip', headers={'test-role': 'pending'})
        self.assertEqual(r.status_code, 500)
        self.assertNotIn('location', r.headers)
        self.assertNotIn('EIP 密碼', r.text)

    async def test_fastapi_exposes_typed_request_schemas_and_static_assets(self):
        paths = self.app.openapi()['paths']
        for path in (
            '/api/v1/eip/link',
            '/api/v1/eip/documents',
            '/api/v1/eip/attachments',
            '/api/v1/eip/process',
            '/api/v1/eip/preview',
            '/api/v1/eip/confirm',
        ):
            self.assertIn('requestBody', paths[path]['post'])
        script = await self.client.get('/eip/assets/app.js')
        stylesheet = await self.client.get('/eip/assets/app.css')
        self.assertEqual(script.status_code, 200)
        self.assertEqual(stylesheet.status_code, 200)

    async def test_status_is_never_served_from_stale_browser_cache(self):
        response = await self.client.get('/api/v1/eip/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_csrf_blocks_cross_origin(self):
        r = await self.client.post(
            '/api/v1/eip/unlink', json={}, headers={'origin': 'https://evil.test'},
        )
        self.assertEqual(r.status_code, 500)
        self.assertIn('alice', self.state.links)

    async def test_sent_folder_available_in_dashboard_and_manual_query(self):
        r = await self.client.get('/eip')
        self.assertIn('<option value="sent">我送出的簽核</option>', r.text)
        self.adapter.documents = AsyncMock(return_value=[])
        r = await self.client.post(
            '/api/v1/eip/documents',
            json={
                'folder': 'sent',
                'start': '2026-09-01',
                'end': '2026-09-06',
                'page': 0,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.adapter.documents.assert_awaited_once_with(
            'sent', '2026-09-01', '2026-09-06', 0,
        )

    async def test_manual_instruction_is_real_editable_prefill(self):
        from html.parser import HTMLParser

        class Textarea(HTMLParser):
            def __init__(self):
                super().__init__()
                self.active = False
                self.text = ''
                self.attrs = {}

            def handle_starttag(self, tag, attrs):
                if tag == 'textarea' and dict(attrs).get('id') == 'instruction':
                    self.active = True
                    self.attrs = dict(attrs)

            def handle_endtag(self, tag):
                if tag == 'textarea':
                    self.active = False

            def handle_data(self, data):
                if self.active:
                    self.text += data

        r = await self.client.get('/eip')
        self.assertEqual(r.status_code, 200)
        field = Textarea()
        field.feed(r.text)
        self.assertEqual(
            field.text, '產生 Word、PDF 與行事曆，另於每項期限後七天提醒我追蹤進度。',
        )
        self.assertNotIn('readonly', field.attrs)
        self.assertNotIn('disabled', field.attrs)

    async def test_manual_query_after_restart_restores_saved_binding(self):
        self.auto.bind('alice', '1709', 'saved-account', 'saved-password')
        original = self.auto.account('alice')
        self.state.links.clear()
        self.adapter.login = AsyncMock()
        self.adapter.documents = AsyncMock(return_value=[])
        with patch.object(self.module, 'Netask', return_value=self.adapter):
            for _ in range(2):
                r = await self.client.post(
                    '/api/v1/eip/documents',
                    json={
                        'folder': 'inbox',
                        'start': '2026-09-01',
                        'end': '2026-09-06',
                        'page': 0,
                    },
                )
                self.assertEqual(r.status_code, 200, r.text)
        self.adapter.login.assert_awaited_once_with('saved-account', 'saved-password')
        self.assertEqual(self.auto.account('alice'), original)
        r = await self.client.get('/api/v1/eip/status')
        self.assertTrue(r.json()['session']['saved_credentials'])
        self.assertNotIn('saved-password', r.text)

    async def test_restore_requires_csrf_and_does_not_use_another_users_binding(self):
        self.auto.bind('alice', '1709', 'saved-account', 'saved-password')
        self.adapter.login = AsyncMock()
        with patch.object(self.module, 'Netask', return_value=self.adapter):
            r = await self.client.post(
                '/api/v1/eip/session/restore',
                json={},
                headers={'origin': 'https://evil.test'},
            )
            self.assertEqual(r.status_code, 500)
            r = await self.client.post(
                '/api/v1/eip/session/restore', json={}, headers={'test-owner': 'bob'},
            )
            self.assertEqual(r.status_code, 500)
        self.adapter.login.assert_not_awaited()

    async def test_preview_never_writes_and_confirmation_deduplicates(self):
        token = await self.preview()
        self.adapter.commit.assert_not_awaited()
        for _ in range(2):
            r = await self.client.post(
                '/api/v1/eip/confirm', json={'token': token, 'confirm': True},
            )
            self.assertEqual(r.status_code, 200, r.text)
        self.adapter.commit.assert_awaited_once()
        self.assertEqual(r.json(), {'created': 0, 'skipped': 1})

    async def test_confirmation_owner_isolation(self):
        token = await self.preview()
        r = await self.client.post(
            '/api/v1/eip/confirm',
            json={'token': token, 'confirm': True},
            headers={'test-owner': 'bob'},
        )
        self.assertEqual(r.status_code, 500)
        self.adapter.commit.assert_not_awaited()

    async def test_confirmation_required_and_bound_to_login(self):
        token = await self.preview()
        r = await self.client.post('/api/v1/eip/confirm', json={'token': token})
        self.assertEqual(r.status_code, 422)
        self.state.links['alice'] = Link(self.adapter, 'account-b')
        r = await self.client.post(
            '/api/v1/eip/confirm', json={'token': token, 'confirm': True},
        )
        self.assertEqual(r.status_code, 500)
        self.adapter.commit.assert_not_awaited()

    async def test_timeout_is_not_retried(self):
        token = await self.preview()
        self.adapter.commit.side_effect = ValueError('網路失敗')
        for _ in range(2):
            r = await self.client.post(
                '/api/v1/eip/confirm', json={'token': token, 'confirm': True},
            )
            self.assertEqual(r.status_code, 500)
        self.adapter.commit.assert_awaited_once()

    async def test_no_attachment_id_guessing(self):
        self.adapter.attachments = AsyncMock()
        r = await self.client.post('/api/v1/eip/attachments', json={'handle': '54696'})
        self.assertEqual(r.status_code, 500)
        self.adapter.attachments.assert_not_awaited()

    async def test_changed_remote_content_stops(self):
        self.adapter.stage.return_value = [remote({**row(), 'Description': 'changed'})]
        with patch.object(
            integration,
            'read_csv',
            AsyncMock(return_value=([row()], csv_bytes([row()]))),
        ):
            r = await self.client.post('/api/v1/eip/preview', json={'file_id': 'owned'})
        self.assertEqual(r.status_code, 500)
        self.adapter.commit.assert_not_awaited()

    async def test_background_job_rechecks_downloads_and_processes_owned_files(self):
        doc = {'id': '54696', 'folder': 'sign'}
        attachment = {
            'name': 'example.pdf',
            'url': 'http://eip.jiedar.com.tw/fo/example.pdf',
        }
        self.state.links['alice'].attachments['opaque'] = {
            **attachment,
            'document': doc,
        }
        self.adapter.attachments = AsyncMock(return_value=[attachment])
        self.adapter.download = AsyncMock(return_value=b'%PDF-test')
        with (
            patch.object(
                integration,
                'source_file',
                AsyncMock(return_value=types.SimpleNamespace(id='owned-id')),
            ) as source,
            patch.object(
                integration,
                'process',
                AsyncMock(
                    return_value={
                        'message': '公文處理完成',
                        'status': 'done',
                        'files': [],
                        'errors': [],
                    },
                ),
            ) as process,
        ):
            r = await self.client.post(
                '/api/v1/eip/process',
                json={'handles': ['opaque'], 'instruction': '產生 Word 與 CSV'},
            )
            self.assertEqual(r.status_code, 200, r.text)
            await asyncio.gather(*self.state.tasks)
            result = await self.client.get('/api/v1/eip/jobs/' + r.json()['job_id'])
            self.assertEqual(result.json()['status'], 'done')
            self.assertEqual(source.await_args.args[0], 'alice')
            self.assertEqual(
                process.await_args.args[:3], ('alice', ['owned-id'], '產生 Word 與 CSV'),
            )
            self.assertEqual(process.await_args.kwargs['groups'], [['owned-id']])
            other = await self.client.get(
                '/api/v1/eip/jobs/' + r.json()['job_id'], headers={'test-owner': 'bob'},
            )
            self.assertEqual(other.status_code, 500)
            self.adapter.commit.assert_not_awaited()

    async def test_grouping_comes_from_owned_handles_and_partial_is_not_done(self):
        attachments = []
        for index, flow in enumerate(['first', 'first', 'second']):
            item = {
                'name': f"a{index}.pdf",
                'url': f"http://eip.jiedar.com.tw/fo/a{index}.pdf",
            }
            attachments.append(item)
            self.state.links['alice'].attachments[f"h{index}"] = {
                **item,
                'document': {'id': flow, 'folder': 'inbox'},
            }
        self.adapter.attachments = AsyncMock(return_value=attachments)
        self.adapter.download = AsyncMock(return_value=b'%PDF-test')
        result = {
            'message': '部分完成',
            'status': 'partial',
            'files': [{'id': 'owned-output', 'name': 'a.docx'}],
            'errors': ['CSV 驗證失敗'],
        }
        with (
            patch.object(
                integration,
                'source_file',
                AsyncMock(
                    side_effect=[types.SimpleNamespace(id=f"id{i}") for i in range(3)],
                ),
            ),
            patch.object(
                integration, 'process', AsyncMock(return_value=result),
            ) as process,
        ):
            rejected = await self.client.post(
                '/api/v1/eip/process',
                json={
                    'handles': ['h0', 'h1', 'h2'],
                    'instruction': '處理',
                    'groups': [['forged']],
                },
            )
            self.assertEqual(rejected.status_code, 422)
            r = await self.client.post(
                '/api/v1/eip/process',
                json={
                    'handles': ['h0', 'h1', 'h2'],
                    'instruction': '處理',
                },
            )
            self.assertEqual(r.status_code, 200)
            await asyncio.gather(*self.state.tasks)
            self.assertEqual(
                process.await_args.kwargs['groups'], [['id0', 'id1'], ['id2']],
            )
            job = await self.client.get('/api/v1/eip/jobs/' + r.json()['job_id'])
            self.assertEqual(job.json()['status'], 'partial')
            self.assertEqual(job.json()['files'], result['files'])
            self.assertEqual(job.json()['errors'], result['errors'])
            self.adapter.commit.assert_not_awaited()

    async def test_pending_reminder_preserves_attention_status_and_structured_result(
        self,
    ):
        attachment = {
            'name': 'example.pdf',
            'url': 'http://eip.jiedar.com.tw/fo/example.pdf',
        }
        self.state.links['alice'].attachments['opaque'] = {
            **attachment,
            'document': {'id': '54696', 'folder': 'sign'},
        }
        self.adapter.attachments = AsyncMock(return_value=[attachment])
        self.adapter.download = AsyncMock(return_value=b'%PDF-test')
        output = {
            'message': '文件完成，提醒待確認',
            'summary': '文件完成，提醒待確認',
            'status': 'attention',
            'files': [],
            'errors': [],
            'warnings': ['沒有期限，未建立相對提醒'],
            'notices': ['未新增行程'],
        }
        with (
            patch.object(
                integration,
                'source_file',
                AsyncMock(return_value=types.SimpleNamespace(id='owned-id')),
            ),
            patch.object(integration, 'process', AsyncMock(return_value=output)),
        ):
            response = await self.client.post(
                '/api/v1/eip/process',
                json={'handles': ['opaque'], 'instruction': '期限後七天提醒'},
            )
            self.assertEqual(response.status_code, 200)
            await asyncio.gather(*self.state.tasks)
            job = (
                await self.client.get('/api/v1/eip/jobs/' + response.json()['job_id'])
            ).json()
            for field in ('status', 'summary', 'notices', 'warnings', 'errors'):
                self.assertEqual(job[field], output[field])
            self.assertIn('文件完成，提醒待確認', job['progress'])
            self.adapter.commit.assert_not_awaited()

    async def test_automation_requires_explicit_opt_in_after_verified_login(self):
        self.adapter.login = AsyncMock()
        with patch.object(self.module, 'Netask', return_value=self.adapter):
            r = await self.client.post(
                '/api/v1/eip/link',
                json={
                    'account': 'name',
                    'password': 'secret',
                    'accept_http': True,
                    'enable_auto': True,
                },
            )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.auto.account('alice')['enabled'])
        status = await self.client.get('/api/v1/eip/status')
        self.assertNotIn('secret', status.text)
        self.assertNotIn('password', status.text)
        self.assertTrue(status.json()['automatic']['enabled'])
        other = await self.client.get(
            '/api/v1/eip/status', headers={'test-owner': 'bob'},
        )
        self.assertIsNone(other.json()['automatic'])

    async def test_failed_login_never_saves_credentials(self):
        self.adapter.login = AsyncMock(side_effect=ValueError('login failed'))
        with patch.object(self.module, 'Netask', return_value=self.adapter):
            r = await self.client.post(
                '/api/v1/eip/link',
                json={
                    'account': 'name',
                    'password': 'secret',
                    'accept_http': True,
                    'enable_auto': True,
                },
            )
        self.assertEqual(r.status_code, 500)
        self.assertIsNone(self.auto.account('alice'))

    async def test_unlink_removes_automatic_credential(self):
        self.auto.bind('alice', '1709', 'name', 'secret')
        r = await self.client.post('/api/v1/eip/unlink', json={})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.auto.account('alice'))

    async def test_pause_route_requires_same_origin_and_only_own_account(self):
        self.auto.bind('alice', '1709', 'name', 'secret')
        r = await self.client.post(
            '/api/v1/eip/automatic/pause',
            json={},
            headers={'origin': 'https://evil.test'},
        )
        self.assertEqual(r.status_code, 500)
        self.assertTrue(self.auto.account('alice')['enabled'])
        r = await self.client.post('/api/v1/eip/automatic/pause', json={})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.auto.account('alice')['enabled'])


if __name__ == '__main__':
    unittest.main()
