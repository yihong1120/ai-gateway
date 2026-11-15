"""Generic app resources, metadata and configuration have no domain contracts."""
from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from company_agent.api.service import ServiceConfiguration
from company_agent.config import Settings
from company_agent.runtime.results import execution_summary
from company_agent.runtime.tools import tool_receipt
from company_agent.services.external_tools.config import MCPServerConfig
from company_agent.services.external_tools.connection import external_tools_connection


class ExternalMCPWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_feature_never_reads_host_tokens(self):
        async with external_tools_connection(SimpleNamespace(), []) as client:
            self.assertIsNone(client)

    async def test_enabled_feature_requires_host_oidc(self):
        with self.assertRaisesRegex(ValueError, 'OIDC'):
            async with external_tools_connection(SimpleNamespace(state=SimpleNamespace()), [self.server()]):
                self.fail('missing host')

    def server(self):
        return MCPServerConfig(name='archive', url='https://archive.example/mcp', allowed_tools=['lookup'])

    def test_settings_snapshot_preserves_generic_server_allowlist(self):
        configuration = ServiceConfiguration(Path('/tmp/state'), Path('/tmp/sources'))
        configuration._configured = Settings(state_dir='/tmp/state', mcp_servers=[self.server()])
        configuration._model_name = 'test-model'
        selected = configuration.settings({'owner': 'alice', 'id': 'job', 'payload': {}})
        self.assertEqual(selected.mcp_servers, [self.server()])
        with self.assertRaises(ValueError):
            Settings(state_dir='/tmp/state', mcp_servers=[self.server(), self.server()])

    def test_public_projection_forwards_opaque_provenance_without_domain_payload(self):
        receipt = {
            'source': 'https://archive.example/mcp', 'server': 'archive', 'tool': 'lookup',
            'queried_at': '2026-11-12T10:00:00+00:00',
            'result': {'raw_document': 'private-source-text', 'provenance': {'coverage': 'partial'}},
        }
        summary = execution_summary({'messages': [tool_receipt('call', 'external_archive_lookup', receipt)]})
        self.assertEqual(summary['read_results'][0]['provenance'], {'coverage': 'partial'})
        self.assertNotIn('private-source-text', str(summary))
        parent = execution_summary({'reports': {'child': summary}})
        self.assertEqual(parent['read_results'], summary['read_results'])

    def test_domain_implementation_does_not_exist_in_gateway(self):
        import company_agent
        root = Path(company_agent.__file__).parent
        self.assertFalse((root/'services/construction/client.py').exists())
        self.assertNotIn('construction_api_url', Settings.model_fields)
        self.assertNotIn('construction_my_sites', (root/'agents/knowledge/__init__.py').read_text())
