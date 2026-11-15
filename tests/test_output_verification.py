"""HTTP and storage boundaries reject the same identity and content changes."""
from __future__ import annotations

import copy
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from examples.official_document_workflow.application.output_receipts import (
    OutputReceipt,
)
from examples.official_document_workflow.application.output_review import (
    WebUIOutputVerifier,
)
from examples.official_document_workflow.services.file_access import FileAccess


class SharedVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_adapters_enforce_name_owner_size_and_content(self):
        data = b'owned bytes'
        receipt = OutputReceipt(
            'file',
            'owner',
            'unknown',
            'file.pdf',
            hashlib.sha256(data).hexdigest(),
            'source',
            kind='document_pdf',
            size=len(data),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'file.pdf'
            path.write_bytes(data)
            base = {
                'id': 'file',
                'user_id': 'owner',
                'filename': 'file.pdf',
                'meta': {
                    'name': 'file.pdf',
                    'file_hash': receipt.sha256,
                    'data': {
                        'source_file_id': 'source',
                        'kind': 'document_pdf',
                        'generated_by': 'official_document_processor_pipe',
                    },
                },
            }
            for change in (
                'valid',
                'name',
                'owner',
                'source',
                'content',
                'too_large',
                'size',
            ):

                item = copy.deepcopy(base)
                content, expected = data, receipt
                if change == 'name':
                    item['meta']['name'] = 'other.pdf'
                if change == 'owner':
                    item['user_id'] = 'other'
                if change == 'source':
                    item['meta']['data']['source_file_id'] = 'other'
                if change == 'content':
                    content = b'wrong bytes'
                if change == 'too_large':
                    content = data + b'extra'
                if change == 'size':
                    expected = replace(receipt, size=len(data) + 1)
                path.write_bytes(content)
                row = SimpleNamespace(**item, path=str(path))
                files = SimpleNamespace(
                    get_file_by_id_and_user_id=AsyncMock(return_value=row),
                )
                storage = SimpleNamespace(get_file=lambda value: value)
                local = await FileAccess(files, storage).inspect(expected)

                def request(request):
                    if request.url.path == '/api/v1/auths/':
                        return httpx.Response(
                            200, json={'id': 'admin', 'role': 'admin'},
                        )
                    return (
                        httpx.Response(200, content=content)
                        if request.url.path.endswith('/content')
                        else httpx.Response(200, json=item)
                    )

                async with httpx.AsyncClient(
                    base_url='http://webui', transport=httpx.MockTransport(request),
                ) as client:
                    remote = await WebUIOutputVerifier(client).verify(expected)
                with self.subTest(change=change):
                    self.assertEqual(local.outcome, remote.outcome)
                    self.assertEqual(
                        local.outcome, 'verified' if change == 'valid' else 'mismatch',
                    )
                    if change != 'valid':
                        self.assertEqual(local.reason, remote.proof['reason'])

    async def test_unavailable_storage_is_not_reported_as_missing(self):
        files = SimpleNamespace(
            get_file_by_id_and_user_id=AsyncMock(
                side_effect=OSError('private diagnostics'),
            ),
        )
        result = await FileAccess(files, None).inspect(None)
        self.assertEqual(result.outcome, 'unavailable')
        self.assertNotIn('private diagnostics', str(result))
