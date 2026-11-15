"""Simplified exception paths still reject bad input and release resources."""
from __future__ import annotations

import io
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import patch

import httpx
from langchain_core.messages import ToolMessage

from company_agent.agents.reviewer.tools import inspected_artifacts
from company_agent.runtime.completion import review_evidence
from company_agent.runtime.middleware import visible_sources
from company_agent.runtime.results import execution_summary
from company_agent.services.eip.client import document_link
from company_agent.services.files.nas import NASClient
from company_agent.services.files.operations import worker
from company_agent.services.files.errors import RecoverableToolError
from company_agent.services.office.documents import spreadsheet_books
from company_agent.services.office.revision import require_inspected
from company_agent.services.research.web import tavily_request
from company_agent.services.sandbox import run_bounded


class ReceiptTests(unittest.TestCase):
    def test_prompt_sources_follow_tool_parameters_and_recheck_access(self):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                roots=[], max_file_bytes=100, nas_binding_revision='revision',
            ),
            store=Mock(),
            owner='alice',
        )
        with patch('company_agent.runtime.middleware.file_access') as access:
            for role in ('coordinator', 'eip', 'research', 'knowledge', 'new_specialist'):
                self.assertEqual(visible_sources(ctx, {'accessed_roots': ['nas_project']}, role, set()), [])
            access.assert_not_called()

        remote = access.return_value
        remote.visible_roots.return_value = [{'name': 'nas_project'}]
        with patch('company_agent.runtime.middleware.file_access', return_value=remote):
            for role in ('knowledge', 'eip', 'new_specialist'):
                remote.allowed.reset_mock()
                self.assertEqual(
                    visible_sources(ctx, {'accessed_roots': ['nas_project']}, role, {'root'}),
                    [{'name': 'nas_project'}],
                )
                remote.allowed.assert_called_once_with('nas_project')

    def test_invalid_canonical_receipt_raises_native_json_error(self):
        message = ToolMessage(name='read_cad', tool_call_id='read', content='not JSON')
        with self.assertRaises(json.JSONDecodeError):
            execution_summary({'messages': [message]})

    def test_malformed_receipt_never_grants_review_or_assignment(self):
        for content in ('bad', '[]', 'null', '42', '"text"', []):
            with self.subTest(content=content):
                message = ToolMessage(
                    name='delegate_reviewer', tool_call_id='d', content=content,
                )
                self.assertEqual(review_evidence({'messages': [message]}), [])
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    inspected_artifacts([message.model_copy(update={'name': 'inspect_artifact'})])
                args = SimpleNamespace(
                    artifact_id='file', sha256='sha', location='p:0', expected='text',
                )
                receipt = message.model_copy(update={'name': 'inspect_artifact'})
                with self.assertRaises((ValueError, TypeError, AttributeError, RecoverableToolError)):
                    require_inspected([receipt], args)

    def test_bad_read_receipts_do_not_become_evidence(self):
        for name in ('read_cad', 'eip_read_attachment'):
            with self.assertRaises(json.JSONDecodeError):
                execution_summary({'messages': [ToolMessage(name=name, tool_call_id='read', content='bad')]})

    def test_eip_view_link_escapes_title_and_keeps_relative_destination(self):
        url = '/agents/eip/235504b6-35ac-4168-8473-5c253209af58/' + 'a' * 32
        link = document_link('測試[公文]', url)
        self.assertEqual(link, r'[測試\[公文\]](' + url + ')')


class WorkbookLifetimeTests(unittest.TestCase):
    def book(self):
        return Mock(sheetnames=['工程'], worksheets=[], close=Mock())

    def test_success_closes_both_books_on_exit_not_before_use(self):
        a, b = self.book(), self.book()
        with (
            patch('company_agent.services.office.documents.spreadsheet_package'),
            patch('openpyxl.load_workbook', side_effect=[a, b]),
        ):
            with spreadsheet_books(b'synthetic') as books:
                self.assertEqual(books, (a, b))
                a.close.assert_not_called()
                b.close.assert_not_called()
        a.close.assert_called_once()
        b.close.assert_called_once()

    def test_second_open_failure_closes_first_and_preserves_exception(self):
        a = self.book()
        failure = RuntimeError('second open failed')
        with (
            patch('company_agent.services.office.documents.spreadsheet_package'),
            patch('openpyxl.load_workbook', side_effect=[a, failure]),
        ):
            with self.assertRaises(RuntimeError) as raised:
                with spreadsheet_books(b'synthetic'):
                    self.fail('must not yield partial workbook pair')
        self.assertIs(raised.exception, failure)
        a.close.assert_called_once()

    def test_consumer_failure_closes_both_and_preserves_exception(self):
        a, b = self.book(), self.book()
        failure = ValueError('no write')
        with (
            patch('company_agent.services.office.documents.spreadsheet_package'),
            patch('openpyxl.load_workbook', side_effect=[a, b]),
        ):
            with self.assertRaises(ValueError) as raised:
                with spreadsheet_books(b'synthetic'):
                    raise failure
        self.assertIs(raised.exception, failure)
        a.close.assert_called_once()
        b.close.assert_called_once()

    def test_closing_one_book_fails_but_other_is_still_closed(self):
        a, b = self.book(), self.book()
        b.close.side_effect = OSError('close failed')
        with (
            patch('company_agent.services.office.documents.spreadsheet_package'),
            patch('openpyxl.load_workbook', side_effect=[a, b]),
        ):
            with self.assertRaises(OSError):
                with spreadsheet_books(b'synthetic'):
                    pass
        a.close.assert_called_once()
        b.close.assert_called_once()


class WorkerBoundaryTests(unittest.TestCase):
    def test_subprocess_error_propagates_without_recapture(self):
        failure = subprocess.CalledProcessError(1, ['parser'], stderr=b'parse failure')
        with patch('company_agent.services.files.operations.subprocess.run', side_effect=failure) as invoke:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                worker('extract', data=b'data', suffix='.docx')
            self.assertIs(caught.exception, failure)
            self.assertTrue(invoke.call_args.kwargs['check'])
        invoke.assert_called_once()

    def test_protocol_failures_propagate_without_retry(self):
        import binascii
        for payload, error in ((b'not json', json.JSONDecodeError), (b'\xff', UnicodeDecodeError), (b'{"binary":"@@"}', binascii.Error), (b'{}', KeyError)):
            run = SimpleNamespace(returncode=0, stdout=payload)
            with patch('company_agent.services.files.operations.subprocess.run', return_value=run) as invoke:
                with self.assertRaises(error):
                    worker('extract', data=b'data', suffix='.docx')
            invoke.assert_called_once()

    def test_timeout_cleans_exact_container_once(self):
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = [subprocess.TimeoutExpired('docker', 60), 0]
        process.poll.return_value = None
        selector = MagicMock()
        selector.__enter__.return_value = selector
        selector.get_map.return_value = {}
        with (
            patch('company_agent.services.sandbox.subprocess.Popen', return_value=process),
            patch(
                'company_agent.services.sandbox.selectors.DefaultSelector',
                return_value=selector,
            ),
            patch('company_agent.services.sandbox.subprocess.run') as cleanup,
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(['docker', 'run'], 'only-this-test-container')
        self.assertEqual(
            cleanup.call_args.args[0], ['docker', 'rm', '-f', 'only-this-test-container'],
        )
        cleanup.assert_called_once()
        process.kill.assert_called_once()
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)


class RemoteBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_nas_auth_error_keeps_its_type(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={'success': False}),
        )
        client = NASClient('https://nas.example.com', 'test', 'secret', transport=transport)
        try:
            with self.assertRaises(PermissionError):
                client._json('SYNO.API.Auth', 'login')
        finally:
            client.close()

    def test_nas_invalid_json_or_nonobject_propagates(self):
        for raw, error in ((b'not json', json.JSONDecodeError), (b'\xff', UnicodeDecodeError), (b'[]', ValueError)):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, content=raw))
            client = NASClient('https://nas.example.com', 'test', 'secret', transport=transport)
            try:
                with self.assertRaises(error):
                    client._json('SYNO.FileStation.Info', 'get')
            finally:
                client.close()

    async def test_tavily_invalid_encoding_propagates(self):
        client_type = httpx.AsyncClient
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b'\xffPRIVATE'),
        )
        with patch(
            'company_agent.services.research.web.httpx.AsyncClient',
            side_effect=lambda **kwargs: client_type(transport=transport, **kwargs),
        ):
            with self.assertRaises(UnicodeDecodeError):
                await tavily_request('search', {'query': 'question'}, 'secret')
