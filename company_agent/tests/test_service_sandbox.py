"""Shared container contracts; mocks execute no Docker or external writes."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from company_agent.services.cad.sandbox import cad_worker
from company_agent.services.office.sandbox import office_worker


class ServiceSandboxTests(unittest.TestCase):
    def run_fake(self, command, container):
        self.command = command
        self.assertEqual(command[command.index('--name') + 1], container)
        self.assertTrue(container.startswith('company-sandbox-'))
        mounts = [command[i + 1] for i, value in enumerate(command) if value == '--mount']
        self.incoming = Path(mounts[0].split(',src=')[1].split(',dst=')[0])
        self.outgoing = Path(mounts[1].split(',src=')[1].split(',dst=')[0])
        self.assertTrue(mounts[0].endswith(',dst=/input,readonly'))
        self.assertTrue(mounts[1].endswith(',dst=/out'))
        self.assertEqual(self.incoming.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.outgoing.stat().st_mode & 0o777, 0o700)
        request = json.loads((self.incoming / 'request.json').read_text())
        self.assertEqual(request, self.expected_request)
        if self.expected_data is not None:
            self.assertEqual((self.incoming / ('source' + self.suffix)).read_bytes(), self.expected_data)
        else:
            self.assertEqual([p.name for p in self.incoming.iterdir()], ['request.json'])
        if self.output:
            target = self.outgoing / self.output
            if self.output_mode == 'valid':
                target.write_bytes(b'generated-output')
            elif self.output_mode == 'empty':
                target.touch()
            elif self.output_mode == 'symlink':
                target.symlink_to(self.incoming / 'request.json')
            elif self.output_mode == 'oversized':
                with target.open('wb') as stream:
                    stream.truncate(20_000_001)
        return self.response

    def setUp(self):
        self.expected_data = None
        self.output_mode = 'valid'
        self.response = b'{"ok":true}'
        self.suffix = '.pdf'
        self.output = 'report.xlsx'
        self.expected_request = {'operation': 'report', 'suffix': '.pdf', 'title': 'report'}

    def test_cad_and_office_share_limits_but_keep_distinct_entrypoints(self):
        cases = (
            ('report', '.pdf', None, 'report.xlsx', {'operation': 'report', 'suffix': '.pdf', 'title': 'report'}),
            ('ocr_page', '.png', b'input-image', 'page.png', {'operation': 'ocr_page', 'suffix': '.png', 'page': 2}),
            (
                'read', '.dwg', b'AC1032fake', None, {
                    'operation': 'read',
                    'view': 'summary', 'start': 0, 'limit': 10, 'query': '',
                },
            ),
            (
                'create', '.dxf', None, 'result.dxf', {
                    'operation': 'create', 'view': 'summary', 'start': 0, 'limit': 10, 'query': '', 'units': 'm', 'entities': [],
                },
            ),
        )
        for operation, self.suffix, self.expected_data, self.output, self.expected_request in cases:
            with self.subTest(operation=operation), patch('company_agent.services.sandbox.run_bounded', side_effect=self.run_fake):
                office = operation in {'report', 'ocr_page'}
                worker = office_worker if office else cad_worker
                args = {'title': 'report'} if operation == 'report' else {'page': 2} if operation == 'ocr_page' else {
                    'units': 'm', 'entities': [],
                } if operation == 'create' else {}
                result = worker(operation, data=self.expected_data, suffix=self.suffix, **args)
                self.assertEqual(result, ({'ok': True}, b'generated-output') if self.output else {'ok': True})
                for option, value in {'--pull': 'never', '--network': 'none', '--cap-drop': 'ALL', '--security-opt': 'no-new-privileges', '--memory': '2g', '--memory-swap': '2g', '--cpus': '1', '--pids-limit': '128', '--user': f'{os.getuid()}:{os.getgid()}'}.items():
                    self.assertEqual(self.command[self.command.index(option) + 1], value)
                self.assertIn('--read-only', self.command)
                self.assertNotIn('--env', self.command)
                self.assertEqual(
                    self.command[self.command.index('--tmpfs') + 1],
                    f'/tmp:rw,noexec,nosuid,size={256 if office else 64}m',
                )
                if office:
                    self.assertEqual(self.command[-1], 'ai-gateway/company-office:1')
                    self.assertNotIn('--entrypoint', self.command)
                else:
                    self.assertEqual(self.command[-2], 'ai-gateway/company-cad:1')
                    self.assertEqual(
                        self.command[-1], '/app/CadReader.dll' if self.suffix ==
                        '.dwg' else '/app/dxf_worker.py',
                    )
                self.assertFalse(self.incoming.parent.exists())

    def test_missing_empty_oversized_and_symlink_outputs_are_not_returned(self):
        for self.output_mode in ('missing', 'empty', 'oversized', 'symlink'):
            with self.subTest(mode=self.output_mode), patch('company_agent.services.sandbox.run_bounded', side_effect=self.run_fake):
                with self.assertRaisesRegex(ValueError, '有效'):
                    office_worker('report', title='report')
                self.assertFalse(self.outgoing.parent.exists())

    def test_invalid_office_response_is_sanitized_and_temp_files_removed(self):
        for self.response in (b'{bad', b'\xff'):
            with self.subTest(response=self.response), patch('company_agent.services.sandbox.run_bounded', side_effect=self.run_fake):
                with self.assertRaises(ValueError) as caught:
                    office_worker('report', title='report')
                self.assertNotIn('private-input', str(caught.exception))
                self.assertFalse(self.incoming.parent.exists())

    def test_invalid_office_inputs_do_not_launch_container(self):
        with patch('company_agent.services.sandbox.run_bounded') as runner:
            for operation, kwargs in (('shell', {}), ('ocr_page', {}), ('ocr_page', {'data': b'x', 'suffix': '.exe'})):
                with self.assertRaises(ValueError):
                    office_worker(operation, **kwargs)
            runner.assert_not_called()
