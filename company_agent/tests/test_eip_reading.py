from __future__ import annotations

import asyncio
import io
import threading
import unittest
from contextlib import asynccontextmanager
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from company_agent.runtime.context import Context
from company_agent.services.eip.client import remember
from company_agent.services.eip.reading import read_attachment
from company_agent.services.eip.schemas import EIPRead
from company_agent.tests.test_agent import Fixture


class EIPReadingTests(unittest.IsolatedAsyncioTestCase, Fixture):
    def setUp(self):
        super().setUp()
        self.config.eip_binding_revision = 'rev'
        self.config.enable_ocr = True
        self.ctx = Context('alice', self.job, self.config, self.store)
        self.att = {'name': '公文.doc', 'url': 'private-url'}
        self.handle = remember(
            self.ctx, {
                'kind': 'attachment', 'doc': {
                    'id': 'd', 'folder': 'inbox',
                }, 'attachment': self.att,
            },
        )
        self.downloads = []
        self.streams = []

        async def read(operation, *args):
            if operation == 'attachments':
                return [self.att]
            self.downloads.append(args)
            stream = io.BytesIO(b'original')
            self.streams.append(stream)
            return stream

        async def convert(*args):
            stream = io.BytesIO(b'pdf')
            self.streams.append(stream)
            return stream, '公文.pdf'

        @asynccontextmanager
        async def session(*args): yield SimpleNamespace(read=read)
        patches = [
            patch('company_agent.services.eip.reading.binding', new=AsyncMock()), patch(
                'official_document_workflow.infrastructure.eip.sessions.manual_session', new=session,
            ),
            patch('official_document_workflow.infrastructure.office.office_input.to_pdf', new=AsyncMock(side_effect=convert)),
            patch(
                'company_agent.services.files.operations.worker', return_value=[
                    {'location': f'page:{i}', 'text': 'body'} for i in range(8)
                ],
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    async def test_pagination_downloads_once_no_derived_files_or_cloud(self):
        one = await read_attachment(self.ctx, EIPRead(handle=self.handle))
        two = await read_attachment(self.ctx, EIPRead(handle=self.handle, start=one['next_start']))
        self.assertEqual(one['next_start'], 5)
        self.assertIsNone(two['next_start'])
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.store.files('alice', self.job), [])
        self.assertEqual(self.store.attempts(self.job), 0)
        self.assertNotIn('private-url', str(one))
        self.assertTrue(all(s.closed for s in self.streams))

    async def test_source_owner_and_binding_change_rejected_before_download(self):
        with self.assertRaises(ValueError):
            await read_attachment(Context('bob', self.job, self.config, self.store), EIPRead(handle=self.handle))
        self.config.eip_binding_revision = 'new'
        with self.assertRaises(ValueError):
            await read_attachment(self.ctx, EIPRead(handle=self.handle))
        self.assertFalse(self.downloads)

    async def test_ocr_requires_explicit_mode_reuses_snapshot(self):
        await read_attachment(self.ctx, EIPRead(handle=self.handle))
        with patch('company_agent.services.office.ocr.ocr_page', return_value={'blocks': [{'location': 'page:2', 'text': '掃描內容'}], 'notice': 'OCR需核對'}) as ocr:
            result = await read_attachment(self.ctx, EIPRead(handle=self.handle, mode='ocr', page=2))
            self.assertEqual(ocr.call_args.args, (b'pdf', '.pdf', 2))
            self.assertEqual(result['page'], 2)
        self.assertEqual(len(self.downloads), 1)

    async def test_parse_failure_is_not_replayed_or_changed_to_ocr(self):
        with patch('company_agent.services.files.operations.worker', side_effect=ValueError('failed')) as parse:
            with self.assertRaises(ValueError):
                await read_attachment(self.ctx, EIPRead(handle=self.handle))
            with self.assertRaises(ValueError):
                await read_attachment(self.ctx, EIPRead(handle=self.handle))
            self.assertEqual(parse.call_count, 1)

    async def test_calendar_attachment_uses_detail_and_downloads_once_for_pagination(self):
        from company_agent.tests.test_calendar_tools import row, detail_rows
        from official_document_workflow.infrastructure.eip.calendar_read import detail
        rows = detail_rows()
        attachment = detail(rows, row()[2])['attachments'][0]
        handle = remember(
            self.ctx, {
                'kind': 'attachment', 'doc': {
                    'kind': 'calendar_event', 'id': row()[2],
                }, 'attachment': attachment,
            },
        )
        raw = io.BytesIO(b'original')
        read = AsyncMock(side_effect=[rows, raw])
        @asynccontextmanager
        async def session(*args): yield SimpleNamespace(read=read)
        with patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            first = await read_attachment(self.ctx, EIPRead(handle=handle))
            await read_attachment(self.ctx, EIPRead(handle=handle, start=first['next_start']))
        self.assertEqual([c.args[0] for c in read.await_args_list], ['calendar_detail', 'download'])
        self.assertEqual(read.await_args.args, ('download', attachment['url'], attachment['name']))
        self.assertEqual(self.store.files('alice', self.job), [])
        self.assertTrue(raw.closed)

    async def test_stream_is_closed_when_conversion_fails_or_input_exceeds_limit(self):
        failure = ValueError('conversion failed')
        with patch('official_document_workflow.infrastructure.office.office_input.to_pdf', new=AsyncMock(side_effect=failure)):
            with self.assertRaises(ValueError) as raised:
                await read_attachment(self.ctx, EIPRead(handle=self.handle))
        self.assertIs(raised.exception, failure)
        self.assertTrue(all(s.closed for s in self.streams))
        raw = io.BytesIO(b'x'*(self.config.max_file_bytes+1))
        read = AsyncMock(side_effect=[[self.att], raw])
        @asynccontextmanager
        async def session(*args): yield SimpleNamespace(read=read)
        handle = remember(
            self.ctx, {
                'kind': 'attachment', 'doc': {
                    'id': 'd', 'folder': 'inbox',
                }, 'attachment': self.att,
            },
        )
        with patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            with self.assertRaisesRegex(ValueError, '單檔上限'):
                await read_attachment(self.ctx, EIPRead(handle=handle))
        self.assertTrue(raw.closed)

    async def test_removed_calendar_attachment_is_not_downloaded(self):
        from company_agent.tests.test_calendar_tools import row, detail_rows
        handle = remember(
            self.ctx, {
                'kind': 'attachment', 'doc': {
                    'kind': 'calendar_event', 'id': row()[
                        2
                    ],
                }, 'attachment': {'name': 'removed.pdf', 'url': '/fo/old.pdf'},
            },
        )
        read = AsyncMock(return_value=detail_rows())
        @asynccontextmanager
        async def session(*args): yield SimpleNamespace(read=read)
        with patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            with self.assertRaisesRegex(ValueError, '附件清單已變更'):
                await read_attachment(self.ctx, EIPRead(handle=handle))
        read.assert_awaited_once_with('calendar_detail', row()[2])

    @contextmanager
    def queries(self):
        original_db = self.store.db
        queries = []

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, query, *args, **kwargs):
                queries.append(query)
                return self.conn.execute(query, *args, **kwargs)

        @contextmanager
        def observed_db(**kwargs):
            with original_db(**kwargs) as conn:
                yield Connection(conn)

        with patch.object(self.store, 'db', observed_db):
            yield queries

    async def test_cached_pagination_reads_no_pdf_bytes_or_ddl(self):
        first = await read_attachment(self.ctx, EIPRead(handle=self.handle))
        with self.queries() as queries, patch('company_agent.services.files.operations.worker') as parser:
            last = await read_attachment(self.ctx, EIPRead(handle=self.handle, start=5))
        self.assertEqual(last['sha256'], first['sha256'])
        self.assertEqual(
            [b['location'] for b in last['blocks']],
            ['page:5/offset:0', 'page:6/offset:0', 'page:7/offset:0'],
        )
        self.assertFalse(any('eip_read_inputs' in q or 'CREATE TABLE' in q for q in queries))
        parser.assert_not_called()
        self.assertEqual(len(self.downloads), 1)

    async def test_cached_source_still_rejects_other_owner_job_or_revision(self):
        await read_attachment(self.ctx, EIPRead(handle=self.handle))
        for ctx in (
            Context('bob', self.job, self.config, self.store),
            Context('alice', self.store.job('alice'), self.config, self.store),
        ):
            with self.assertRaises(ValueError):
                await read_attachment(ctx, EIPRead(handle=self.handle, start=5))
        self.config.eip_binding_revision = 'changed'
        with self.assertRaises(ValueError):
            await read_attachment(self.ctx, EIPRead(handle=self.handle, start=5))
        self.assertEqual(len(self.downloads), 1)

    async def test_binding_revoked_before_cached_page_return(self):
        await read_attachment(self.ctx, EIPRead(handle=self.handle))
        revoked = ValueError('binding revoked')
        with patch('company_agent.services.eip.reading.binding', new=AsyncMock(side_effect=[None, revoked])) as check:
            with self.assertRaises(ValueError) as raised:
                await read_attachment(self.ctx, EIPRead(handle=self.handle, start=5))
        self.assertIs(raised.exception, revoked)
        self.assertEqual(check.await_count, 2)
        self.assertEqual(len(self.downloads), 1)

    async def test_cached_ocr_requires_current_enablement(self):
        with patch('company_agent.services.office.ocr.ocr_page', return_value={'blocks': [], 'notice': 'empty'}):
            await read_attachment(self.ctx, EIPRead(handle=self.handle, mode='ocr'))
        self.config.enable_ocr = False
        with self.assertRaisesRegex(ValueError, 'OCR 未啟用'):
            await read_attachment(self.ctx, EIPRead(handle=self.handle, mode='ocr'))
        self.assertEqual(len(self.downloads), 1)

    async def test_unicode_chunks_and_empty_blocks_preserve_exact_text(self):
        text = '漢' * 4001
        blocks = [{'location': 'page:1', 'text': text}, {'location': 'page:2', 'text': ''}]
        with patch('company_agent.services.files.operations.worker', return_value=blocks):
            result = await read_attachment(self.ctx, EIPRead(handle=self.handle))
        self.assertEqual(
            [b['location'] for b in result['blocks']],
            ['page:1/offset:0', 'page:1/offset:4000', 'page:2/offset:0'],
        )
        self.assertEqual(''.join(b['text'] for b in result['blocks']), text)
        self.assertEqual(result['total_blocks'], 3)
        self.assertIsNone(result['next_start'])

    async def test_oversized_extracted_text_fails_without_replay(self):
        with patch('company_agent.services.files.operations.worker', return_value=[{'location': 'page:1', 'text': 'x' * 1_000_000}]) as parser:
            with self.assertRaisesRegex(ValueError, '附件文字超過上限'):
                await read_attachment(self.ctx, EIPRead(handle=self.handle))
            with self.assertRaisesRegex(ValueError, '停止重送'):
                await read_attachment(self.ctx, EIPRead(handle=self.handle))
        self.assertEqual(parser.call_count, 1)
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.store.files('alice', self.job), [])

    async def test_slow_database_does_not_block_status_event_loop(self):
        await read_attachment(self.ctx, EIPRead(handle=self.handle))
        entered, release = threading.Event(), threading.Event()
        original = self.store.begin_tool

        def delayed(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Event loop could not release database worker')
            return original(*args, **kwargs)

        with patch.object(self.store, 'begin_tool', side_effect=delayed):
            task = asyncio.create_task(read_attachment(self.ctx, EIPRead(handle=self.handle, start=5)))
            try:
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        await asyncio.sleep(.01)
                release.set()
                result = await asyncio.wait_for(task, 10)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(result['blocks']), 3)
