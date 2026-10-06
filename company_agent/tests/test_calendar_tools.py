from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from langchain_core.tools import ToolException
from pydantic import ValidationError

from company_agent.agents.eip.tools import eip_tools
from company_agent.agents.registry import ROLE_PROMPTS
from company_agent.runtime.context import Context
from company_agent.runtime.execution import DocumentAgent
from company_agent.services.eip.calendar import calendar_source
from company_agent.services.eip.client import query
from company_agent.services.eip.client import remember
from company_agent.services.eip.client import source
from company_agent.services.eip.drafts import prepare
from company_agent.services.eip.schemas import CalendarEvent
from company_agent.services.eip.schemas import CalendarPrepare
from company_agent.services.eip.schemas import CalendarQuery
from company_agent.services.eip.schemas import EIPAttachments
from company_agent.tests.test_agent import call
from company_agent.tests.test_agent import Fixture
from company_agent.tests.test_enterprise_completion import finish
from company_agent.tests.test_enterprise_completion import RawRoleModel
from official_document_workflow.infrastructure.eip.calendar_csv import parse_csv
from official_document_workflow.infrastructure.eip.calendar_read import detail
from official_document_workflow.infrastructure.eip.calendar_read import occurrences
from official_document_workflow.infrastructure.eip.netask import js_escape
from official_document_workflow.infrastructure.eip.netask import Netask


def event(**kw):
    return dict(
        subject='測試會議', start_date='2026-09-18', end_date='2026-09-18',
        all_day=False, start_time='09:00', end_time='10:00', **kw,
    )


def row():
    r = ['']*21
    r[2] = '2026-09-01 10:00:00;123'
    r[6] = js_escape('會議 😀')
    r[7] = 'new Date(2026, 8, 18, 0, 0, 0)'
    r[8] = '0'
    r[9] = '540'
    r[10] = '600'
    return r


def detail_rows():
    r = ['']*43
    r[0] = row()[2]
    r[8] = row()[6]
    r[14] = js_escape('會勘地點：本工程現場\n原文未載明結束時間；系統預留 60 分鐘。')
    return [
        r, [2], ['17', 'file-a', '通知.pdf', '/fo/private/a.pdf', 'site', '100', ''],
        ['17', 'file-b', '地點.pdf', '/fo/private/b.pdf', 'site', '200', ''],
        ['17', '回報紀錄不是附件', '0', '2026-09-01', '100'],
    ]


class CalendarContracts(unittest.TestCase):
    def test_dates_times_missing_reversed_and_injected_parameters(self):
        CalendarQuery(start='2026-09-01', end='2026-09-30')
        CalendarEvent(**event())
        CalendarEvent(subject='全天', start_date='2026-09-18', end_date='2026-09-19', all_day=True)
        for update in (
            {'end_time': ''}, {'end_time': '08:59'}, {'start_time': '25:00'},
            {'all_day': True}, {'owner': 'someone_else'}, {'private': False},
            {'start_date': '20260918'}, {'subject': ' '}, {'subject': '=CMD()'},
            {'description': '\x00'},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                CalendarEvent(**{**event(), **update})
        with self.assertRaises(ValidationError):
            CalendarQuery(start='2026-09-01', end='2027-09-01')
        with self.assertRaises(ValidationError):
            CalendarPrepare(events=[CalendarEvent(**event())]*2)

    def test_real_row_contract_unicode_and_no_eval(self):
        r = occurrences([row()], '2026-09-01', '2026-09-30')[0]
        self.assertEqual(
            r, {
                'id': row()[2], 'subject': '會議 😀', 'date': '2026-09-18',
                'weekday': '星期五', 'all_day': False, 'start_time': '09:00', 'end_time': '10:00',
            },
        )
        full = row()
        full[8] = '1'
        full[9] = ''
        full[10] = ''
        self.assertIsNone(occurrences([full], '2026-09-01', '2026-09-30')[0]['start_time'])
        self.assertEqual(occurrences([row()], '2026-09-01', '2026-09-02'), [])
        for bad in ['new Date(2026, 8, 18, 0, 0, 0);fetch("evil")', 'new Date(2026, 19, 18, 0, 0, 0)']:
            r = row()
            r[7] = bad
            with self.assertRaises(ValueError):
                occurrences([r], '2026-09-01', '2026-09-30')
        with self.assertRaises(ValueError):
            occurrences([row()]*501, '2026-09-01', '2026-09-30')
        with self.assertRaises(ValueError):
            occurrences([['workflow row']], '2026-09-01', '2026-09-30')

    def test_tools_have_no_commit_or_owner_override(self):
        tools = {t.name: t for t in eip_tools()}
        self.assertIn('eip_calendar_query', tools)
        self.assertIn('eip_calendar_prepare', tools)
        self.assertIn('eip_calendar_detail', tools)
        self.assertFalse(any('confirm' in n or 'commit' in n for n in tools))
        self.assertIn('不能用 eip_query', ROLE_PROMPTS['eip'])

    def test_detail_decodes_content_and_counted_attachments_without_report_rows(self):
        result = detail(detail_rows(), row()[2])
        self.assertEqual(result['subject'], '會議 😀')
        self.assertEqual(result['description'], '會勘地點：本工程現場\n原文未載明結束時間；系統預留 60 分鐘。')
        self.assertEqual([a['name'] for a in result['attachments']], ['通知.pdf', '地點.pdf'])
        with self.assertRaises(ValueError):
            detail(detail_rows(), 'another-event')
        with self.assertRaises(ValueError):
            detail(detail_rows()[:3], row()[2])
        empty = detail_rows()
        empty[1] = [0]
        empty = empty[:2]
        self.assertEqual(detail(empty, row()[2])['attachments'], [])


class CalendarAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_exact_cm_contract_never_workflow_or_write(self):
        n = Netask()
        n.rpc = AsyncMock(return_value=[['0'], row()])
        try:
            self.assertEqual(len(await n.calendar('2026-09-01', '2026-09-30')), 1)
            n.rpc.assert_awaited_once_with(
                'List', ap_name='cm', start_date='2026-09-01',
                end_date='2026-09-30', __tag='event_list',
            )
        finally:
            await n.close()


class CalendarToolTests(unittest.IsolatedAsyncioTestCase, Fixture):
    async def test_pipeline_download_format_survives_artifact_copy_and_preview(self):
        from tests.test_eip_calendar_csv import fixture
        from official_document_workflow.infrastructure.eip.calendar_csv import export_eip_csv
        from official_document_workflow.domain.calendar.csv_schema import CALENDAR_CSV_FORMAT
        import hashlib

        rows, _ = parse_csv(fixture())
        payload = export_eip_csv(rows)
        artifact = self.store.artifact('alice', self.job, '公文批次_3組_行事曆.csv', payload)
        with self.store.db() as db:
            db.execute(
                'INSERT INTO eip_calendar_sources(owner,job,artifact,revision,csv_format) VALUES(%s,%s,%s,%s,%s)',
                ('alice', self.job, artifact['id'], 'rev', CALENDAR_CSV_FORMAT),
            )
        revision, sha, decoded = calendar_source(self.store, 'alice', artifact['id'])
        self.assertEqual(revision, 'rev')
        self.assertEqual(sha, hashlib.sha256(payload).hexdigest())
        imported, _ = parse_csv(decoded)
        self.assertEqual(imported[0]['Description'], rows[0]['Description'])
        self.assertIn('2K+500', imported[0]['Subject'])
        self.assertIn('%2B', imported[0]['Subject'])

    async def test_upgrade_preserves_legacy_calendar_sources_as_plain(self):
        from company_agent.runtime.storage import Store
        from tests.test_eip_calendar_csv import fixture

        artifact = self.store.artifact('alice', self.job, 'legacy.csv', fixture())
        with self.store.db() as db:
            db.execute('DROP TABLE eip_calendar_sources')
            db.execute(
                'CREATE TABLE eip_calendar_sources(owner TEXT,job TEXT,artifact TEXT,revision TEXT, PRIMARY KEY(owner,job,artifact))',
            )
            db.execute(
                'INSERT INTO eip_calendar_sources VALUES(%s,%s,%s,%s)',
                ('alice', self.job, artifact['id'], 'rev'),
            )
        upgraded = Store(self.private)
        with upgraded.db() as db:
            self.assertEqual(db.execute('SELECT csv_format FROM eip_calendar_sources').fetchone()[0], 'plain')
        self.assertEqual(calendar_source(upgraded, 'alice', artifact['id'])[2], fixture())

    def setUp(self):
        super().setUp()
        from tests.test_eip_connector import SCHEMA
        mock = patch('official_document_workflow.application.integration.pipe_module', return_value=SCHEMA)
        mock.start()
        self.addCleanup(mock.stop)

    def ctx(self):
        self.config.eip_binding_revision = 'rev'
        return Context('alice', self.job, self.config, self.store)

    async def test_draft_is_private_owned_eligible_cached_and_not_written(self):
        args = CalendarPrepare(events=[CalendarEvent(**event())])
        ctx = self.ctx()
        with patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch.object(Netask, 'commit', new=AsyncMock()) as commit:
            result = await prepare(ctx, args)
            self.assertEqual(await prepare(ctx, args), result)
            commit.assert_not_awaited()
        self.assertEqual(result['status'], 'prepared_not_written')
        self.assertEqual(len(self.store.files('alice', self.job)), 1)
        self.assertEqual(result['events'][0]['start_weekday'], '星期五')
        revision, _sha, payload = calendar_source(self.store, 'alice', result['files'][0]['id'])
        rows, transport = parse_csv(payload)
        self.assertEqual(revision, 'rev')
        self.assertEqual(rows[0]['Private'], '真')
        self.assertEqual(rows[0]['Start Time(24hr)'], '09:00')
        self.assertIn('上午09:00:00', transport.decode('utf-8-sig'))
        with self.assertRaises(ValueError):
            calendar_source(self.store, 'bob', result['files'][0]['id'])
        rogue = self.store.artifact('alice', self.job, 'arbitrary.csv', payload)
        with self.assertRaises(ValueError):
            calendar_source(self.store, 'alice', rogue['id'])
        with patch('company_agent.services.eip.client.binding', new=AsyncMock(side_effect=ValueError('binding changed'))):
            with self.assertRaises(ValueError):
                await prepare(ctx, args)

    async def test_calendar_query_receipt_and_actual_source(self):
        read = AsyncMock(return_value=occurrences([row()], '2026-09-01', '2026-09-30'))
        @asynccontextmanager
        async def session(*a, **kw): yield SimpleNamespace(read=read)
        args = CalendarQuery(start='2026-09-01', end='2026-09-30')
        ctx = self.ctx()
        with patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            result = await query(ctx, 'eip_calendar_query', args, 'q')
            self.assertEqual(await query(ctx, 'eip_calendar_query', args, 'q'), result)
        read.assert_awaited_once_with('calendar', '2026-09-01', '2026-09-30')
        self.assertEqual(result['source'], 'eip_calendar')
        self.assertNotIn('documents', result)
        self.assertNotIn('id', result['events'][0])
        self.assertEqual(source(ctx, result['events'][0]['handle'], 'calendar_event')['id'], row()[2])

    async def test_detail_uses_owned_list_handle_and_reuses_result_across_call_ids(self):
        read = AsyncMock(side_effect=[occurrences([row()], '2026-09-01', '2026-09-30'), detail_rows()])
        @asynccontextmanager
        async def session(*a, **kw): yield SimpleNamespace(read=read)
        ctx = self.ctx()
        with patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session):
            listing = await query(ctx, 'eip_calendar_query', CalendarQuery(start='2026-09-01', end='2026-09-30'), 'list')
            args = EIPAttachments(handle=listing['events'][0]['handle'])
            result = await query(ctx, 'eip_calendar_detail', args, 'detail-1')
            self.assertEqual(await query(ctx, 'eip_calendar_detail', args, 'detail-2'), result)
        self.assertEqual(read.await_count, 2)
        self.assertEqual(read.await_args.args, ('calendar_detail', row()[2]))
        self.assertIn('原文未載明結束時間', result['event']['description'])
        self.assertEqual(len(result['attachments']), 2)
        item = source(ctx, result['attachments'][0]['handle'], 'attachment')
        self.assertEqual(item['doc']['kind'], 'calendar_event')
        self.assertEqual(item['doc']['id'], row()[2])
        self.assertNotIn('/fo/private', str(result))
        self.assertEqual(self.store.files('alice', self.job), [])

    async def test_detail_rejects_other_owner_job_kind_and_changed_binding_before_read(self):
        ctx = self.ctx()
        handle = remember(ctx, {'kind': 'calendar_event', 'id': row()[2]})
        args = EIPAttachments(handle=handle)
        contexts = [
            Context('bob', self.job, self.config, self.store),
            Context('alice', self.store.job('alice'), self.config, self.store),
            Context('alice', self.job, self.config.model_copy(update={'eip_binding_revision': 'new'}), self.store),
        ]
        with patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch('official_document_workflow.infrastructure.eip.sessions.manual_session') as session:
            for other in contexts:
                with self.assertRaises(ValueError):
                    await query(other, 'eip_calendar_detail', args, 'detail')
            wrong = remember(ctx, {'kind': 'document', 'id': 'workflow'})
            with self.assertRaises(ValueError):
                await query(ctx, 'eip_calendar_detail', EIPAttachments(handle=wrong), 'wrong')
            session.assert_not_called()

    async def test_graph_reads_detail_after_listing_without_preparing_writes(self):
        import json
        from langchain_core.messages import ToolMessage
        self.config.structured_completion = True
        _ = self.ctx()
        read = AsyncMock(side_effect=[occurrences([row()], '2026-09-01', '2026-09-30'), detail_rows()])
        @asynccontextmanager
        async def session(*a, **kw): yield SimpleNamespace(read=read)

        def select(messages):
            listing = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            return call('eip_calendar_detail', {'handle': listing['events'][0]['handle']}, 'detail')

        def complete(messages):
            result = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
            self.assertIn('會勘地點', result['event']['description'])
            self.assertEqual(len(result['attachments']), 2)
            return finish('complete_assignment')
        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '查這筆會勘詳細內容'}, 'delegate'), finish()],
            eip=[call('eip_calendar_query', {'start': '2026-09-01', 'end': '2026-09-30'}, 'list'), select, complete],
        )
        with patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch('official_document_workflow.infrastructure.eip.sessions.manual_session', session), patch('company_agent.services.eip.drafts.prepare', new=AsyncMock()) as write:
            result = await DocumentAgent(self.config, model).run('alice', '查會議詳細內容')
        self.assertEqual(result['status'], 'done', result)
        write.assert_not_awaited()
        self.assertEqual([c.args[0] for c in read.await_args_list], ['calendar', 'calendar_detail'])

    async def test_graph_query_uses_calendar_without_preparing_writes(self):
        self.config.structured_completion = True
        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '查九月行事曆'}, 'd'), finish()],
            eip=[
                call(
                    'eip_calendar_query', {
                        'start': '2026-09-01',
                        'end': '2026-09-30',
                    }, 'q',
                ), finish('complete_assignment'),
            ],
        )
        read = AsyncMock(return_value={'source': 'eip_calendar', 'events': []})
        with patch('company_agent.services.eip.client.query', new=read), patch('company_agent.services.eip.client.binding', new=AsyncMock()), patch('company_agent.services.eip.drafts.prepare', new=AsyncMock()) as write:
            result = await DocumentAgent(self.config, model).run('alice', '幫我看這個月行事曆')
        self.assertEqual(result['status'], 'done', result)
        write.assert_not_awaited()
        self.assertEqual(read.call_args.args[1], 'eip_calendar_query')

    async def test_graph_prepare_no_commit_and_requires_reviewer(self):
        self.config.structured_completion = True
        self.config.require_artifact_review = True

        def done(messages):
            f = self.store.files('alice', next(iter(self.jobs_seen)))[0]
            return call('complete_assignment', {'outcome': 'completed', 'summary': '行程已準備但未寫入', 'artifact_ids': [f['id']]}, 'done')
        # Exercise real draft with the graph runtime job, then ensure final
        # completion cannot bypass independently reading the resulting CSV.
        self.jobs_seen = set()
        async def bind(ctx): self.jobs_seen.add(ctx.job)

        def final(messages):
            f = self.store.files('alice', next(iter(self.jobs_seen)))[0]
            return call('complete_job', {'outcome': 'completed', 'summary': '準備完成', 'artifact_ids': [f['id']]}, 'final')
        model = RawRoleModel(
            coordinator=[call('delegate_eip', {'task': '準備 9/18 9-10 點測試會議'}, 'd'), final],
            eip=[call('eip_calendar_prepare', {'events': [event()]}, 'p'), done],
        )
        with patch('company_agent.services.eip.client.binding', new=bind), patch.object(Netask, 'commit', new=AsyncMock()) as commit:
            result = await self.failed_job(DocumentAgent(self.config, model).run('alice', '新增測試會議，2026/9/18 09:00 至 10:00，台北時間'), ToolException)
        commit.assert_not_awaited()
        self.assertEqual(result['status'], 'running', result)
        self.assertIn('獨立審核', str(self.native_error))
        self.assertEqual(len(result['files']), 1)

    async def test_complete_draft_review_handoff_never_commits(self):
        self.config.structured_completion = True
        self.config.require_artifact_review = True
        self.config.eip_binding_revision = 'rev'
        jobs = []
        async def bind(ctx): jobs.append(ctx.job)
        def artifact(): return self.store.files('alice', jobs[-1])[0]['id']
        def done(name): return lambda m: finish(name, ids=[artifact()], summary='已準備並檢查 CSV，尚未寫入。')
        model = RawRoleModel(
            coordinator=[
                call('delegate_eip', {'task': '準備行程'}, 'd'),
                lambda m:call(
                    'delegate_reviewer', {
                        'task': '核對行程 CSV',
                        'context': artifact(),
                    }, 'review',
                ), done('complete_job'),
            ],
            eip=[call('eip_calendar_prepare', {'events': [event()]}, 'p'), done('complete_assignment')],
            reviewer=[
                lambda m:call('inspect_artifact', {'artifact_id': artifact()}, 'inspect'), done(
                    'complete_assignment',
                ),
            ],
        )
        with patch('company_agent.services.eip.client.binding', new=bind), patch.object(Netask, 'commit', new=AsyncMock()) as commit, \
                patch.object(model, 'bind_tools', wraps=model.bind_tools) as bindings:
            result = await DocumentAgent(self.config, model).run('alice', '新增 2026/9/18 09:00 至 10:00 的測試會議，台北時間。')
        self.assertEqual(result['status'], 'done', result)
        commit.assert_not_awaited()
        self.assertFalse(result['artifact_review']['unreviewed_ids'])
        self.assertTrue(any(c.kwargs.get('tool_choice') == 'required' for c in bindings.call_args_list))
