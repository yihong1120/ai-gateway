from __future__ import annotations

import copy
import unittest
from unittest.mock import AsyncMock

from official_document_workflow.infrastructure.eip.calendar_attachments import attach_event
from official_document_workflow.infrastructure.eip.calendar_attachments import attachment_parameters
from tests.test_eip_calendar_verification import EVENT
from tests.test_eip_calendar_verification import fixture


class AttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_attachment_preserves_verified_members_and_project(self):
        line, detail, _ = fixture()
        detail[0][4] = '77'
        detail[0][15] = 'Ru1709,Ru1721'
        after = copy.deepcopy(detail)
        after[1] = [1]
        after.append(['1709', '123', '改名後.pdf', '/fo/v/123', '', 3, ''])
        adapter = type('Adapter', (), {'user_id': '1709'})()
        adapter.calendar_detail = AsyncMock(side_effect=[detail, after])
        adapter.upload_calendar_attachment = AsyncMock(
            return_value={'id': '123', 'filename': '改名後.pdf', 'url': '/fo/v/123', 'size': 3},
        )
        adapter.calendar_site_id = AsyncMock(return_value='765')
        adapter.rpc = AsyncMock()
        result = await attach_event(adapter, EVENT, line, [('改名後.pdf', b'pdf')], project_id='77', sharing=['Ru1709', 'Ru1721'])
        self.assertTrue(result['attachment_verified'])
        self.assertEqual(adapter.rpc.call_args.kwargs['proj_id'], '77')
        self.assertEqual(adapter.rpc.call_args.kwargs['pub'], 'Ru1709,Ru1721')
        adapter.calendar_detail = AsyncMock(return_value=detail)
        adapter.upload_calendar_attachment.reset_mock()
        with self.assertRaises(ValueError):
            await attach_event(adapter, EVENT, line, [('改名後.pdf', b'pdf')], project_id='79', sharing=['Ru1709', 'Ru1721'])
        adapter.upload_calendar_attachment.assert_not_awaited()

    def test_update_parameters_preserve_private_import_and_add_tuple(self):
        line, detail, _ = fixture()
        attachment = {'id': '123', 'filename': '改名後.pdf', 'url': '/fo/v/123', 'size': 12}
        params = attachment_parameters(detail, line, '1709', EVENT, '765', [attachment])
        self.assertEqual(params['event_id'], EVENT)
        self.assertEqual(params['att_ownerid[0]'], '1709')
        self.assertEqual(params['att_id[0]'], '123')
        self.assertEqual(params['att_siteid[0]'], '765')
        self.assertEqual(params['att_name[0]'], '改名後.pdf')
        self.assertEqual(params['att_oid[0]'], 'undefined')
        self.assertEqual(params['update_history'], '1')
        with self.assertRaises(ValueError):
            attachment_parameters(detail, line, '1709', EVENT, 'bad', [attachment])
        with self.assertRaises(ValueError):
            attachment_parameters(detail, line, '1709', EVENT, '765', [])

    async def test_attach_requires_readback_of_uploaded_id_and_filename(self):
        line, detail, _ = fixture()
        after = copy.deepcopy(detail)
        after[1] = [1]
        after.append(['1709', '123', '改名後.pdf', '/fo/v/123', '', 12, ''])
        adapter = type('Adapter', (), {})()
        adapter.user_id = '1709'
        adapter.calendar_detail = AsyncMock(side_effect=[detail, after])
        adapter.upload_calendar_attachment = AsyncMock(
            return_value={
                'id': '123', 'filename': '改名後.pdf', 'url': '/fo/v/123', 'size': 3,
            },
        )
        adapter.calendar_site_id = AsyncMock(return_value='765')
        adapter.rpc = AsyncMock(return_value=[[0, '']])
        result = await attach_event(adapter, EVENT, line, [('改名後.pdf', b'pdf')])
        self.assertTrue(result['attachment_verified'])
        self.assertEqual(adapter.rpc.call_args.kwargs['att_name[0]'], '改名後.pdf')

    async def test_missing_detail_attachment_is_not_declared_success(self):
        line, detail, _ = fixture()
        adapter = type('Adapter', (), {})()
        adapter.user_id = '1709'
        adapter.calendar_detail = AsyncMock(side_effect=[detail, detail])
        adapter.upload_calendar_attachment = AsyncMock(
            return_value={
                'id': '123', 'filename': '改名後.pdf', 'url': '/fo/v/123', 'size': 3,
            },
        )
        adapter.calendar_site_id = AsyncMock(return_value='765')
        adapter.rpc = AsyncMock(return_value=[[0, '']])
        with self.assertRaises(ValueError):
            await attach_event(adapter, EVENT, line, [('改名後.pdf', b'pdf')])
