"""Operator verification reads real bytes and never treats absence as rollback."""
from __future__ import annotations

import hashlib
import unittest

import httpx

from examples.official_document_workflow.application.output_receipts import OutputReceipt
from examples.official_document_workflow.application.output_review import WebUIOutputVerifier


class OutputVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.data = b'Subject,Date\nVerified,2026-11-08\n'
        self.receipt = OutputReceipt(
            'output-id', 'owner', 'unknown', 'file.csv',
            hashlib.sha256(self.data).hexdigest(), 'source',
        )
        self.role, self.status, self.owner, self.content = 'admin', 200, 'owner', self.data
        self.paths = []

    def request(self, request):
        self.paths.append(request.url.path)
        if request.url.path == '/api/v1/auths/':
            return httpx.Response(200, json={'id': 'verified-admin', 'role': self.role})
        if request.url.path.endswith('/content'):
            return httpx.Response(200, content=self.content)
        return httpx.Response(
            self.status, json={
                'id': self.receipt.id, 'user_id': self.owner, 'meta': {
                    'name': self.receipt.name, 'file_hash': self.receipt.sha256, 'data': {
                        'source_file_id': 'source',
                        'kind': 'calendar_csv_pending_reminder', 'generated_by': 'official_document_processor_pipe',
                    },
                },
            },
        )

    async def verify(self):
        async with httpx.AsyncClient(base_url='http://webui.test', transport=httpx.MockTransport(self.request)) as client:
            return await WebUIOutputVerifier(client).verify(self.receipt)

    async def test_success_records_actual_hash_and_server_authenticated_actor(self):
        result = await self.verify()
        self.assertEqual(result.outcome, 'verified')
        self.assertEqual(result.actor, 'verified-admin')
        self.assertEqual(result.proof, {'sha256': self.receipt.sha256, 'bytes': len(self.data)})

    async def test_wrong_owner_and_non_admin_never_download_content(self):
        self.owner = 'other'
        self.assertEqual((await self.verify()).outcome, 'mismatch')
        self.assertFalse(any(p.endswith('/content') for p in self.paths))
        self.paths.clear()
        self.role = 'user'
        with self.assertRaises(PermissionError):
            await self.verify()
        self.assertEqual(self.paths, ['/api/v1/auths/'])

    async def test_declared_hash_does_not_hide_changed_content(self):
        self.content = b'different bytes'
        self.assertEqual((await self.verify()).outcome, 'mismatch')

    async def test_missing_or_unavailable_file_is_not_a_safe_retry_decision(self):
        self.status = 404
        self.assertEqual((await self.verify()).outcome, 'not_found')
        self.status = 503
        result = await self.verify()
        self.assertEqual(result.outcome, 'unavailable')
        self.assertNotIn('content', result.proof)
