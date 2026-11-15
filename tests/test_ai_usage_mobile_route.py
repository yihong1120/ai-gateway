"""The relocated route tool preserves native/Web behavior without live I/O."""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from examples.ai_usage.deployment import mobile_route

BASELINE = (
    'server {\n'
    '    # Existing unrelated site settings remain intact.\n'
    '    location ^~ /hazard/api/ai_usage/ {\n'
    '        proxy_set_header Authorization "";\n'
    '        proxy_pass http://127.0.0.1:8006/bff/ai_usage/;\n'
    '    }\n'
    '}\n'
)


class RouteRunner:
    """Simulate only the subprocesses the tool is allowed to perform."""

    def __init__(self, site, routing_map, *, fail_first_validation=False):
        self.site, self.routing_map = site, routing_map
        self.commands = []
        self.fail_first_validation = fail_first_validation

    def __call__(self, args, **kwargs):
        command = tuple(args)
        self.commands.append(command)
        if command == ('nginx', '-t'):
            status = int(self.fail_first_validation)
            self.fail_first_validation = False
            return subprocess.CompletedProcess(args, status, stdout='', stderr='')
        if command == ('systemctl', 'reload', 'nginx'):
            return subprocess.CompletedProcess(args, 0, stdout='', stderr='')
        if not command or command[0] != 'curl':
            raise AssertionError('Unexpected subprocess: ' + repr(command))
        if command[-1].endswith(mobile_route.ROUTE):
            bearer = 'Authorization: Bearer deliberately-invalid' in command
            applied = self.routing_map.exists() and mobile_route.NEW_MARKER in self.site.read_text()
            detail = 'invalid_access_token' if bearer and applied else 'app_session_expired'
            payload = json.dumps({'detail': {'code': detail}}) + '\n401'
        elif command[-1] == 'https://wiki.changdar-server.mooo.com/':
            payload = 'HTTP/2 302\r\nLocation: /login\r\n\r\n\n302'
        elif command[-1] in ('https://' + mobile_route.HOST + '/', 'https://ai.changdar-server.mooo.com/'):
            payload = 'HTTP/2 200\r\n\r\n\n200'
        else:
            raise AssertionError('Unexpected network target: ' + command[-1])
        return subprocess.CompletedProcess(args, 0, stdout=payload, stderr='')


class MobileRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.site = self.root / 'site.conf'
        self.routing_map = self.root / 'routing-map.conf'
        self.backups = self.root / 'backups'
        self.site.write_text(BASELINE)
        for name, value in (('SITE', self.site), ('MAP', self.routing_map), ('BACKUPS', self.backups)):
            current = patch.object(mobile_route, name, value)
            current.start()
            self.addCleanup(current.stop)

    def invoke(self, runner, *args):
        with (
            patch.object(mobile_route.subprocess, 'run', side_effect=runner),
            patch.object(mobile_route.time, 'time_ns', return_value=1234),
            patch('sys.argv', ['mobile_route.py', *args]),
            redirect_stdout(StringIO()) as output,
        ):
            mobile_route.main()
        return output.getvalue()

    def test_check_probes_real_command_contract_without_writes_or_reload(self):
        runner = RouteRunner(self.site, self.routing_map)
        with patch.object(mobile_route.shutil, 'copy2') as copy:
            output = self.invoke(runner, '--check')
        self.assertIn('Ready:', output)
        self.assertEqual(self.site.read_text(), BASELINE)
        self.assertFalse(self.routing_map.exists())
        self.assertFalse(self.backups.exists())
        copy.assert_not_called()
        self.assertEqual(len(runner.commands), 5)
        self.assertTrue(all(command[0] == 'curl' for command in runner.commands))
        self.assertTrue(all('--resolve' in command and '--noproxy' in command for command in runner.commands))
        self.assertTrue(all('--location' not in command and '-L' not in command for command in runner.commands))
        self.assertEqual(runner.commands[0][-1], 'https://' + mobile_route.HOST + mobile_route.ROUTE)
        self.assertNotIn('--header', runner.commands[0])
        self.assertIn('Authorization: Bearer deliberately-invalid', runner.commands[1])

    def test_apply_preserves_cookie_and_bearer_routing_and_verifies_both(self):
        runner = RouteRunner(self.site, self.routing_map)
        output = self.invoke(runner)
        self.assertIn('Native AI usage Bearer routing verified', output)
        self.assertEqual((self.backups / 'backup-1234/phase6-main.conf').read_text(), BASELINE)
        self.assertEqual(self.routing_map.read_text(), mobile_route.MAP_TEXT)
        expected = BASELINE.replace(mobile_route.BLOCK.findall(BASELINE)[0], mobile_route.NEW_BLOCK, 1)
        self.assertEqual(self.site.read_text(), expected)
        self.assertIn('~*^Bearer\\s+\\S+$ 1;', self.routing_map.read_text())
        self.assertIn('0 http://127.0.0.1:8006;', self.routing_map.read_text())
        self.assertIn('1 http://127.0.0.1:8012;', self.routing_map.read_text())
        self.assertIn('0 /bff/ai_usage/;', self.routing_map.read_text())
        self.assertIn('proxy_set_header Cookie $http_cookie;', self.site.read_text())
        self.assertIn('proxy_set_header Authorization $http_authorization;', self.site.read_text())
        self.assertNotIn(mobile_route.OLD_MARKER, self.site.read_text())
        self.assertEqual(runner.commands.count(('nginx', '-t')), 1)
        self.assertEqual(runner.commands.count(('systemctl', 'reload', 'nginx')), 1)
        probes = [c for c in runner.commands if c[0] == 'curl' and c[-1].endswith(mobile_route.ROUTE)]
        self.assertEqual(len(probes), 4)  # Web/native before and after the change.

    def test_failed_nginx_validation_restores_site_and_removes_only_new_map(self):
        runner = RouteRunner(self.site, self.routing_map, fail_first_validation=True)
        with self.assertRaisesRegex(RuntimeError, 'nginx failed'):
            self.invoke(runner)
        self.assertEqual(self.site.read_text(), BASELINE)
        self.assertFalse(self.routing_map.exists())
        self.assertEqual((self.backups / 'backup-1234/phase6-main.conf').read_text(), BASELINE)
        self.assertEqual(runner.commands.count(('nginx', '-t')), 2)
        self.assertEqual(runner.commands.count(('systemctl', 'reload', 'nginx')), 1)

    def test_existing_map_refuses_before_probes_or_writes(self):
        self.routing_map.write_text('operator-owned map')
        runner = RouteRunner(self.site, self.routing_map)
        with self.assertRaisesRegex(RuntimeError, 'map already exists'):
            self.invoke(runner, '--check')
        self.assertEqual(self.routing_map.read_text(), 'operator-owned map')
        self.assertEqual(self.site.read_text(), BASELINE)
        self.assertFalse(self.backups.exists())
        self.assertFalse(runner.commands)

    def test_changed_location_refuses_before_probes_or_writes(self):
        self.site.write_text(
            BASELINE.replace(
                mobile_route.OLD_MARKER,
                'proxy_set_header Authorization $http_authorization;',
            ),
        )
        original = self.site.read_text()
        runner = RouteRunner(self.site, self.routing_map)
        with self.assertRaisesRegex(RuntimeError, 'gateway has changed'):
            self.invoke(runner)
        self.assertEqual(self.site.read_text(), original)
        self.assertFalse(self.routing_map.exists())
        self.assertFalse(self.backups.exists())
        self.assertFalse(runner.commands)


if __name__ == '__main__':
    unittest.main()
