"""Ten-file boundaries across manual processing, ownership and EIP uploads."""
from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

from pydantic import ValidationError

from examples.official_document_workflow.api.schemas import ProcessRequest
from examples.official_document_workflow.application import integration
from examples.official_document_workflow.application import multi_agent
from examples.official_document_workflow.infrastructure.eip.calendar_attachments import attach_event
from examples.official_document_workflow.infrastructure.eip.calendar_attachments import attachment_parameters
from tests.test_eip_calendar_verification import EVENT
from tests.test_eip_calendar_verification import fixture


class AttachmentLimitTests(IsolatedAsyncioTestCase):
    def test_manual_request_accepts_ten_and_rejects_eleven_or_duplicate_handles(self):
        handles = [f'attachment-{index}' for index in range(10)]
        self.assertEqual(ProcessRequest(handles=handles).handles, handles)
        for invalid in (handles + ['extra'], handles[:-1] + [handles[0]]):
            with self.assertRaises(ValidationError):
                ProcessRequest(handles=invalid)

    async def test_team_passes_all_ten_owned_files_without_truncating(self):
        files = [SimpleNamespace(id=f'file-{index}') for index in range(10)]
        prepared = [SimpleNamespace(source_file=file, cleanup=Mock()) for file in files]
        ids = [file.id for file in files]
        pipe = SimpleNamespace(
            owned_sources=AsyncMock(return_value=('pdf', files)),
            prepare_owned_source_documents=AsyncMock(return_value=prepared),
        )
        with patch.object(multi_agent, 'BatchWorkflow') as workflow:
            workflow.return_value.run = AsyncMock(return_value={'status': 'done'})
            result = await multi_agent.process_with_team(pipe,  'owner', ids, [ids])
        self.assertEqual(result['status'], 'done')
        self.assertEqual(workflow.call_args.args[2:5], (ids, [ids], prepared))
        self.assertEqual(len(pipe.owned_sources.await_args.args[0]), 10)
        for item in prepared:
            item.cleanup.assert_called_once()

    async def test_team_rejects_eleven_before_file_access(self):
        pipe = SimpleNamespace(owned_sources=AsyncMock())
        ids = [f'file-{index}' for index in range(11)]
        with self.assertRaises(ValueError):
            await multi_agent.process_with_team(pipe,  'owner', ids, [ids])
        pipe.owned_sources.assert_not_awaited()

    async def test_all_ten_calendar_files_require_owner_access_and_are_read(self):
        with TemporaryDirectory() as directory:
            entries, stored = [], {}
            for index in range(10):
                file_id, name = f'file-{index}', f'document-{index}.pdf'
                path = Path(directory) / name
                path.write_bytes(b'%PDF-fixture-' + str(index).encode())
                stored[file_id] = SimpleNamespace(filename=name, path=str(path))
                entries.append({'id': file_id, 'name': name})

            async def owned(file_id, owner):
                self.assertEqual(owner, 'owner')
                return stored[file_id]

            lookup = AsyncMock(side_effect=owned)
            with patch.object(integration, 'Files', SimpleNamespace(get_file_by_id_and_user_id=lookup)), \
                    patch.object(integration, 'Storage', SimpleNamespace(get_file=lambda path: path)):
                result = await integration.read_calendar_attachments('owner', entries)
            self.assertEqual(lookup.await_count, 10)
            self.assertEqual([name for name, _ in result], [entry['name'] for entry in entries])
            self.assertTrue(result[-1][1].endswith(b'9'))

    async def test_eleven_calendar_files_are_rejected_before_owner_lookup(self):
        lookup = AsyncMock()
        with patch.object(integration, 'Files', SimpleNamespace(get_file_by_id_and_user_id=lookup)):
            with self.assertRaises(ValueError):
                await integration.read_calendar_attachments('owner', [{}] * 11)
        lookup.assert_not_awaited()

    async def test_ten_eip_uploads_are_preserved_in_update_and_verified_in_readback(self):
        line, before, _ = fixture()
        after = copy.deepcopy(before)
        after[1] = [10]
        uploaded = []
        for index in range(10):
            item = {'id': str(index + 1), 'filename': f'{index}.pdf', 'url': f'/fo/v/{index}', 'size': 3}
            uploaded.append(item)
            after.append(['1709', item['id'], item['filename'], item['url'], '', 3, ''])
        adapter = SimpleNamespace(
            user_id='1709', calendar_detail=AsyncMock(side_effect=[before, after]),
            upload_calendar_attachment=AsyncMock(side_effect=uploaded),
            calendar_site_id=AsyncMock(return_value='765'), rpc=AsyncMock(),
        )
        result = await attach_event(adapter, EVENT, line, [(item['filename'], b'pdf') for item in uploaded])
        self.assertTrue(result['attachment_verified'])
        self.assertEqual(len(result['files']), 10)
        self.assertEqual(adapter.upload_calendar_attachment.await_count, 10)
        self.assertEqual(adapter.rpc.call_args.kwargs['att_id[9]'], '10')
        self.assertEqual(adapter.rpc.call_args.kwargs['att_name[9]'], '9.pdf')

    async def test_eleven_eip_files_are_rejected_without_upload_or_update(self):
        line, before, _ = fixture()
        adapter = SimpleNamespace(
            user_id='1709', calendar_detail=AsyncMock(return_value=before),
            upload_calendar_attachment=AsyncMock(), rpc=AsyncMock(),
        )
        with self.assertRaises(ValueError):
            await attach_event(adapter, EVENT, line, [('document.pdf', b'pdf')] * 11)
        adapter.upload_calendar_attachment.assert_not_awaited()
        adapter.rpc.assert_not_awaited()
        with self.assertRaises(ValueError):
            attachment_parameters(before, line, '1709', EVENT, '765', [{}] * 11)
