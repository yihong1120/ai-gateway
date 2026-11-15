"""Bridge credentials and target are immutable and verified before use."""
from __future__ import annotations

import unittest
from unittest.mock import patch
import httpx
from examples.company_ai_access.adapters.bridge import BridgeClient, BridgeSettings


class BridgeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_transport_uses_only_its_validated_url_key_and_timeouts(self):
        sent = []
        def handle(request):
            sent.append(request)
            return httpx.Response(200, json={'allowed': True})
        settings = BridgeSettings('http://127.0.0.1:18083', 'k'*40)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            bridge = BridgeClient(settings, http)
            with patch.dict('os.environ', {'AI_ACCESS_BRIDGE_URL': 'https://other.invalid', 'AI_ACCESS_BRIDGE_KEY':'wrong'}):
                await bridge.check_network('192.0.2.1')
                response = await bridge.send('POST', '/company-ai/v1/messages', {'x-company-ai-bridge':'spoofed'}, {})
                await response.aclose()
        self.assertEqual(len(sent), 2)
        for request in sent:
            self.assertEqual(request.url.host, '127.0.0.1')
            self.assertEqual(request.headers.get_list('x-company-ai-bridge'), ['k'*40])
        self.assertEqual(sent[0].extensions['timeout']['read'], 5)
        self.assertEqual(sent[1].extensions['timeout']['read'], 600)

    async def test_settings_reject_credentials_queries_fragments_and_nonloopback_targets(self):
        for value in ('https://127.0.0.1','http://localhost:80','http://user@127.0.0.1',
                      'http://127.0.0.1?','http://127.0.0.1#','http://127.0.0.1/',
                      'http://127.0.0.1:0','http://127.0.0.1:99999'):
            with self.subTest(url=value), self.assertRaises(ValueError):
                BridgeSettings(value, 'k'*40)
        with self.assertRaises(ValueError):
            BridgeSettings('http://127.0.0.1:18083', 'short')
