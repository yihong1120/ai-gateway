"""Approved native Codex -> Company AI Responses -> local MCP -> owned vault.

Both servers, IdP and model are synthetic; no external model or provider key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock
import shutil
import subprocess
import tempfile


from company_agent.tests.test_nas import NASFixture
from tests.company_mcp import test_resources as fixture
from company_mcp.adapters.company import Resources
from tests.company_mcp.runtime import close_client, start
from examples.official_document_workflow.runtime import current_resources
from tests.test_company_ai_codex_cli import test_installed_codex_tool_loop as tool_loop


class NativeMCPTests(NASFixture):
    def test_approved_codex_executes_company_mcp_and_replays_result(self):
        executable = shutil.which('codex')
        if not executable or subprocess.check_output([executable, '--version'], text=True).strip() != 'codex-cli 0.164.0':
            self.skipTest('Run with the approved Codex 0.164.0 binary on PATH.')
        authentication = fixture.OIDCTests()
        authentication.setUp()
        self.addCleanup(close_client, authentication.http)
        self.bind()
        workflow = current_resources()
        resources = Resources(fixture.configuration(), self.store, Mock(return_value=workflow))
        service = start(self, resources, authentication.verifier)
        port = service.port

        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix='company-mcp-native-') as temporary:
            finalizers = []

            class Request:
                addfinalizer = staticmethod(finalizers.append)

            config = '\n'.join([
                '[mcp_servers.changdar_company]',
                'url = ' + json.dumps(f'http://127.0.0.1:{port}/company-mcp'),
                'bearer_token_env_var = "COMPANY_MCP_SYNTHETIC_TOKEN"',
                'startup_timeout_sec = 10',
                '[mcp_servers.changdar_company.http_headers]',
                'Host = "company.example"',
            ]) + '\n'
            with patch.dict(os.environ, {'COMPANY_MCP_SYNTHETIC_TOKEN': authentication.token()}):
                try:
                    tool_loop(Path(temporary), 'gemini-3.8-flash', Request(),
                              mcp_config=config, mcp_tool_suffix='connections_list')
                finally:
                    for finalizer in reversed(finalizers):
                        finalizer()
