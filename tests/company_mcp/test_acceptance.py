"""All 16 tools across MCP HTTP -> private authority -> real owned services.

Only identity-provider and NAS/EIP network boundaries use synthetic fixtures.
PDF validation/extraction, PostgreSQL, ownership, receipts and drafts are real.
No paid model or production-calendar mutation is needed for this acceptance.
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import fitz
import httpx

from company_agent.tests.test_nas import FakeDSM
from company_agent.tests.test_nas import NASFixture
from company_mcp.tools import TOOLS
from tests.company_mcp import test_service as service


class DirectoryDSM(FakeDSM):
    directory_real = '/volume1/homes/alice/subdir'
    directory_mount = ''

    def handler(self, request):
        response = super().handler(request)
        parameters = parse_qs(request.content.decode())
        if parameters['method'] == ['getinfo'] and json.loads(parameters['path'][0]) == ['/home/subdir']:
            data = response.json()
            row = data['data']['files'][0]
            row['isdir'] = True
            row['additional'].update(real_path=self.directory_real, mount_point_type=self.directory_mount)
            return httpx.Response(200, json=data)
        return response


class ToolAcceptanceTests(unittest.IsolatedAsyncioTestCase, NASFixture):
    asyncSetUp = service.ServiceTests.asyncSetUp
    asyncTearDown = service.ServiceTests.asyncTearDown
    token = service.ServiceTests.token
    rpc = service.ServiceTests.rpc

    async def call(self, name, **arguments):
        result = await self.rpc(name, arguments)
        self.assertFalse(result.get('isError', False), result)
        self.successful_tools.add(name)
        return result['structuredContent']

    def download_pdf(self, url, name):
        self.assertEqual((url, name), ('/fo/a.pdf', 'a.pdf'))
        return BytesIO(self.pdf)

    def install_eip_fixture(self):
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), 'MCP acceptance: owned EIP attachment.')
            self.pdf = document.tobytes()
        self.adapter.download = AsyncMock(side_effect=self.download_pdf)
        self.adapter.calendar.return_value = [{
            'id': '42', 'subject': '工程會議', 'date': '2026-11-18', 'weekday': '星期三',
            'all_day': False, 'start_time': '09:00', 'end_time': '10:00',
        }]
        row = [''] * 43
        row[0], row[8], row[14] = '42', '工程會議', '本次會議內文'
        self.adapter.calendar_detail = AsyncMock(
            return_value=[
                row, ['1'], ['', '', 'a.pdf', '/fo/a.pdf', '', '', ''],
            ],
        )

    async def test_all_16_tools_through_independent_http_boundary(self):
        self.successful_tools = set()
        self.bind()
        self.install_eip_fixture()
        connections = await self.call('connections_list')
        self.assertEqual(len(connections['nas']), 1)
        self.assertTrue(connections['eip']['saved'])
        prepared = await self.call('workspace_prepare', request_id='all-tools')
        workspace = prepared['workspace']

        roots = await self.call('nas_roots', workspace=workspace)
        root = next(item['name'] for item in roots['roots'] if item['label'] == 'home')
        listing = await self.call('nas_list', workspace=workspace, root=root)
        self.assertIn('a.txt', str(listing))
        search = await self.call('nas_search', workspace=workspace, root=root, query='a.txt')
        self.assertIn('a.txt', str(search))
        read = await self.call('nas_read', workspace=workspace, root=root, path='a.txt')
        self.assertIn('alice secret', str(read))
        self.assertFalse(self.dsm.sessions)

        documents = await self.call(
            'eip_query', workspace=workspace, folder='inbox',
            start='2026-11-01', end='2026-11-30',
        )
        handle = documents['documents'][0]['handle']
        detail = await self.call('eip_document_read', workspace=workspace, handle=handle)
        self.assertEqual(detail['fields'][0]['value'], '測試')
        attachments = await self.call('eip_attachments', workspace=workspace, handle=handle)
        attachment = attachments['attachments'][0]['handle']
        content = await self.call('eip_read_attachment', workspace=workspace, handle=attachment)
        self.assertIn('MCP acceptance: owned EIP attachment.', str(content))
        again = await self.call('eip_read_attachment', workspace=workspace, handle=attachment)
        self.assertEqual(content, again)
        self.adapter.download.assert_awaited_once()

        calendar = await self.call(
            'eip_calendar_query', workspace=workspace,
            start='2026-11-01', end='2026-11-30',
        )
        event = calendar['events'][0]['handle']
        event_detail = await self.call('eip_calendar_detail', workspace=workspace, handle=event)
        self.assertEqual(event_detail['event']['description'], '本次會議內文')
        calendar_text = await self.call(
            'eip_read_attachment', workspace=workspace,
            handle=event_detail['attachments'][0]['handle'],
        )
        self.assertIn('MCP acceptance: owned EIP attachment.', str(calendar_text))

        draft = await self.call(
            'calendar_plan_prepare', workspace=workspace, events=[{
                'subject': '工程會議', 'start_date': '2026-11-18', 'end_date': '2026-11-18',
                'all_day': False, 'start_time': '09:00', 'end_time': '10:00',
            }],
        )
        self.assertEqual(draft['status'], 'prepared_not_written')
        artifact = await self.call('artifact_get', workspace=workspace, artifact=draft['files'][0]['id'])
        self.assertTrue(artifact['download_url'].startswith(self.configuration.origin + '/agents/artifacts/'))
        self.assertTrue(artifact['sha256'])
        self.assertNotIn('commit', self.adapter.__dict__)

        analysis = await self.call(
            'document_analysis_prepare', workspace=workspace,
            request_id='analysis-once', handles=[attachment],
        )
        self.assertFalse(analysis['model_called'])
        self.assertEqual(self.queue.list('alice'), [])
        operation = analysis['operation']
        status = await self.call('operation_status', operation=operation)
        self.assertEqual(status['status'], 'awaiting_personal_confirmation')
        # The browser-only port is exercised separately by test_browser; it is
        # intentionally absent from the MCP catalog and cannot self-approve.
        job = await self.resources.confirm_analysis(self.alice, operation)
        self.assertEqual(await self.resources.confirm_analysis(self.alice, operation), job)
        queued = await self.call('operation_status', operation=operation)
        self.assertEqual(queued['job']['id'], job)
        self.assertEqual(len(self.queue.list('alice')), 1)
        self.assertEqual(self.successful_tools, set(TOOLS))

    async def test_two_nas_accounts_are_readable_in_one_workspace_without_mixing_home(self):
        self.successful_tools = set()
        first = self.bind()
        with self.store.db() as db:
            db.execute('DELETE FROM nas_login_limits')
        self.bind('alice', 'team')
        workspace = (await self.call('workspace_prepare', request_id='multi-nas'))['workspace']
        roots = await self.call('nas_roots', workspace=workspace)
        homes = [root for root in roots['roots'] if root['label'] == 'home']
        self.assertEqual(len(homes), 2)
        self.assertNotEqual(homes[0]['name'], homes[1]['name'])
        contents = []
        for root in homes:
            result = await self.call('nas_read', workspace=workspace, root=root['name'], path='a.txt')
            contents.append(str(result))
        self.assertTrue(any('alice secret' in value for value in contents))
        self.assertTrue(any('team secret' in value for value in contents))
        self.vault.unlink('alice', first['binding_id'])
        removed = next(root['name'] for root in homes if root['name'].startswith('nas_' + first['revision'][:8]))
        denied = await self.rpc('nas_read', {'workspace': workspace, 'root': removed, 'path': 'a.txt'})
        self.assertTrue(denied['isError'])
        self.assertNotIn('alice secret', str(denied))
        self.assertFalse(self.dsm.sessions)

    async def test_nas_listing_allows_root_and_descendant_but_rejects_escapes_and_mounts(self):
        self.successful_tools = set()
        self.dsm = DirectoryDSM()
        self.bind()
        workspace = (await self.call('workspace_prepare', request_id='listing-guards'))['workspace']
        roots = await self.call('nas_roots', workspace=workspace)
        root = next(item['name'] for item in roots['roots'] if item['label'] == 'home')
        for path in ('', 'subdir'):
            listing = await self.call('nas_list', workspace=workspace, root=root, path=path)
            self.assertEqual(listing['entries'][0]['path'], (path + '/' if path else '') + 'a.txt')
        for real, mount in (
            ('/volume1/homes/alice', ''), ('/volume1/homes/bob/private', ''),
            ('/volume1/homes/alice-other/private', ''), ('/volume1/homes/alice/../bob', ''),
            (None, ''), ('/volume1/homes/alice/subdir', 'remote'),
        ):
            with self.subTest(real=real, mount=mount):
                self.dsm.directory_real, self.dsm.directory_mount = real, mount
                before = len([item for item in self.dsm.calls if item['method'] == 'list'])
                denied = await self.rpc('nas_list', {'workspace': workspace, 'root': root, 'path': 'subdir'})
                self.assertTrue(denied['isError'])
                self.assertEqual(len([item for item in self.dsm.calls if item['method'] == 'list']), before)
                self.assertFalse(self.dsm.sessions)

    async def test_eip_cache_and_artifacts_never_bypass_workspace_ownership_or_rebinding(self):
        self.successful_tools = set()
        self.install_eip_fixture()
        workspace = (await self.call('workspace_prepare', request_id='eip-cache'))['workspace']
        documents = await self.call(
            'eip_query', workspace=workspace, folder='inbox',
            start='2026-11-01', end='2026-11-30',
        )
        attachments = await self.call(
            'eip_attachments', workspace=workspace,
            handle=documents['documents'][0]['handle'],
        )
        attachment = attachments['attachments'][0]['handle']
        await self.call('eip_read_attachment', workspace=workspace, handle=attachment)
        other = (await self.call('workspace_prepare', request_id='different-workspace'))['workspace']
        denied = await self.rpc('eip_read_attachment', {'workspace': other, 'handle': attachment})
        self.assertTrue(denied['isError'])
        foreign = self.store.artifact('bob', self.store.job('bob'), 'private.txt', b'foreign-private-result')
        denied = await self.rpc('artifact_get', {'workspace': workspace, 'artifact': foreign['id']})
        self.assertTrue(denied['isError'])
        self.assertNotIn('foreign-private-result', str(denied))
        self.workflow.store.pause('alice')
        denied = await self.rpc('eip_read_attachment', {'workspace': workspace, 'handle': attachment})
        self.assertTrue(denied['isError'])
        self.assertNotIn('MCP acceptance', str(denied))
        self.adapter.download.assert_awaited_once()
