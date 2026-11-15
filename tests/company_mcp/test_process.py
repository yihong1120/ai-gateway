"""Actual process restart, persisted workspace and independent liveness."""
from __future__ import annotations

from unittest.mock import Mock

import httpx

from company_agent.tests.test_nas import NASFixture
from company_mcp.adapters.company import Resources
from examples.official_document_workflow.runtime import current_resources
from tests.company_mcp import runtime
from tests.company_mcp import test_resources as fixture


class ProcessTests(NASFixture):
    def test_real_edge_restart_preserves_workspace_and_survives_backend_shutdown(self):
        auth = fixture.OIDCTests()
        auth.setUp()
        self.addCleanup(runtime.close_client, auth.http)
        workflow = current_resources()
        resources = Resources(fixture.configuration(), self.store, Mock(return_value=workflow))
        service = runtime.start(self, resources, auth.verifier)
        original_pid = service.process.pid
        headers = {
            'Host': 'company.example', 'Authorization': 'Bearer ' + auth.token(),
            'accept': 'application/json, text/event-stream',
        }
        payload = {
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'workspace_prepare', 'arguments': {'request_id': 'persist-across-process'}},
        }
        with httpx.Client(base_url=f'http://127.0.0.1:{service.port}', trust_env=False) as client:
            first = client.post('/company-mcp', headers=headers, json=payload)
            workspace = first.json()['result']['structuredContent']['workspace']
            service.stop()
            self.assertTrue(service.thread.is_alive())
            service.start()
            self.assertNotEqual(service.process.pid, original_pid)
            second = client.post('/company-mcp', headers=headers, json=payload)
            self.assertEqual(second.json()['result']['structuredContent']['workspace'], workspace)
            current_pid = service.process.pid
            service.stop_backend()
            self.assertEqual(client.get('/healthz').status_code, 200)
            self.assertEqual(client.get('/readyz').status_code, 503)
            failed = client.post(
                '/company-mcp', headers=headers,
                json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
            )
            self.assertEqual(failed.status_code, 503)
            self.assertIsNone(service.process.poll())
            self.assertEqual(service.process.pid, current_pid)
