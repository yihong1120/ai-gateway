"""Service isolation, explicit overrides and safe one-time configuration import."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from examples.shared.config import (
    ConfigurationError,
    execution_environment,
    load_service,
    read_service,
    read_services,
)

from examples.deployment.cli import main, migrate


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'config.toml'
        self.config.write_text(
            '[webui]\nDATABASE_URL = "web-db"\nOPENAI_API_KEYS = "chat-key"\nENABLED = false\n'
            'EMPTY = ""\nJSON_VALUE = "[]"\n'
            '[litellm]\nLITELLM_MASTER_KEY = "private-master"\n'
            '[identity]\nDATABASE_URL = "identity-db"\n',
        )
        self.enterContext(patch.dict(os.environ, {'AI_GATEWAY_CONFIG': str(self.config)}, clear=True))

    def test_each_service_gets_only_its_section_and_preserves_scalar_meaning(self):
        values = read_service('webui')
        self.assertEqual(values['DATABASE_URL'], 'web-db')
        self.assertEqual(values['ENABLED'], 'false')
        self.assertEqual(values['EMPTY'], '')
        self.assertEqual(values['JSON_VALUE'], '[]')
        self.assertNotIn('LITELLM_MASTER_KEY', values)
        self.assertEqual(read_service('identity')['DATABASE_URL'], 'identity-db')
        self.assertEqual(read_service('missing'), {})

    def test_shared_reader_import_has_no_deployment_dependencies_or_environment_changes(self):
        result = subprocess.run(
            [sys.executable, '-c',
             'import os,sys; before=dict(os.environ); import examples.shared.config; '
             'assert dict(os.environ)==before; '
             'assert not any(n.startswith("examples.deployment") for n in sys.modules); '
             'assert "argparse" not in sys.modules; '
             'assert "dotenv" not in sys.modules'],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_environment_wins_and_loading_does_not_export_other_sections(self):
        os.environ['DATABASE_URL'] = 'explicit-db'
        load_service('webui')
        self.assertEqual(os.environ['DATABASE_URL'], 'explicit-db')
        self.assertEqual(os.environ['OPENAI_API_KEYS'], 'chat-key')
        self.assertNotIn('LITELLM_MASTER_KEY', os.environ)
        self.assertNotIn('PYTHON_DOTENV_DISABLED', os.environ)

    def test_isolated_tasks_can_disable_implicit_loading_but_explicit_files_work(self):
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        with patch('examples.shared.config.read_configuration') as read:
            self.assertEqual(read_service('webui'), {})
            read.assert_not_called()
        self.assertEqual(read_service('webui', self.config)['DATABASE_URL'], 'web-db')

    def test_exec_expands_argv_without_a_shell_or_other_service_credentials(self):
        output = StringIO()
        with patch('examples.deployment.cli.os.execvpe') as execute, redirect_stdout(output):
            main(['--config', str(self.config), 'exec', 'webui', '--', 'server', '{DATABASE_URL}'])
        command, argv, environment = execute.call_args.args
        self.assertEqual((command, argv), ('server', ['server', 'web-db']))
        self.assertEqual(environment['PYTHON_DOTENV_DISABLED'], '1')
        self.assertEqual(environment['AI_GATEWAY_CONFIG'], str(self.config))
        self.assertNotIn('LITELLM_MASTER_KEY', environment)
        self.assertEqual(output.getvalue(), '')

    def test_invalid_file_and_missing_argv_value_do_not_echo_secrets(self):
        self.config.write_text('[webui]\nPASSWORD = "private-secret\n')
        error = StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit):
            main(['--config', str(self.config), 'check'])
        self.assertNotIn('private-secret', error.getvalue())
        self.config.write_text('[webui]\nPASSWORD = "private-secret"\n')
        with redirect_stderr(error), self.assertRaises(SystemExit):
            main(['--config', str(self.config), 'exec', 'webui', '--', 'server', '{UNSET}'])
        self.assertNotIn('private-secret', error.getvalue())

    def test_csv_and_json_are_explicit_strings_and_nested_tables_are_rejected(self):
        self.config.write_text('[webui]\nMODEL_NAMES = ["one", "two"]\n')
        with self.assertRaises(ConfigurationError):
            read_service('webui')
        self.config.write_text('[webui]\nVALUE = inf\n')
        with self.assertRaises(ConfigurationError):
            read_service('webui')

    def test_migration_preserves_duplicate_names_by_service_and_refuses_overwrite(self):
        web = self.root / '.env.openwebui'
        web.write_text('DATABASE_URL=web-db\nVALUE="a # b"\n')
        lite = self.root / 'examples/litellm/.env'
        lite.parent.mkdir(parents=True)
        lite.write_text('DATABASE_URL=lite-db\nLITELLM_MASTER_KEY=private-master\n')
        snapshots = {file: file.read_bytes() for file in (web, lite)}
        destination = self.root / 'private.toml'
        counts = migrate(self.root, destination)
        self.assertEqual(counts, {'webui': 2, 'litellm': 2})
        self.assertEqual(read_service('webui', destination)['DATABASE_URL'], 'web-db')
        self.assertEqual(read_service('litellm', destination)['DATABASE_URL'], 'lite-db')
        self.assertEqual(read_service('webui', destination)['VALUE'], 'a # b')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        before = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            migrate(self.root, destination)
        self.assertEqual(destination.read_bytes(), before)
        for file, snapshot in snapshots.items():
            self.assertEqual(file.read_bytes(), snapshot)

    def test_child_environment_uses_selected_file_even_with_a_different_parent_path(self):
        another = self.root / 'other.toml'
        another.write_text('[webui]\nDATABASE_URL = "other-db"\n')
        env = execution_environment('webui', another)
        self.assertEqual(env['DATABASE_URL'], 'other-db')
        self.assertEqual(env['AI_GATEWAY_CONFIG'], str(another))

    def test_multi_service_read_uses_one_snapshot_without_exporting_other_sections(self):
        from examples.shared.config import read_configuration

        original = read_configuration

        def replaced_after_read(path):
            document = original(path)
            self.config.write_text('[webui]\nDATABASE_URL="replacement-db"\n')
            return document

        with patch('examples.shared.config.read_configuration', side_effect=replaced_after_read) as read:
            sections = read_services('webui', 'litellm', path=self.config)
        read.assert_called_once_with(self.config.resolve())
        self.assertEqual(sections['webui']['DATABASE_URL'], 'web-db')
        self.assertEqual(sections['litellm']['LITELLM_MASTER_KEY'], 'private-master')
        self.assertNotIn('identity', sections)
        self.assertNotIn('LITELLM_MASTER_KEY', sections['webui'])

    def test_exec_does_not_mix_configuration_versions_between_validation_and_projection(self):
        from examples.shared.config import read_configuration

        original = read_configuration

        def replaced_after_read(path):
            document = original(path)
            self.config.write_text('[identity]\nDATABASE_URL="replacement-db"\n')
            return document

        with patch('examples.shared.config.read_configuration', side_effect=replaced_after_read) as read:
            environment = execution_environment('webui', self.config)
        read.assert_called_once_with(self.config.resolve())
        self.assertEqual(environment['DATABASE_URL'], 'web-db')
        self.assertEqual(environment['OPENAI_API_KEYS'], 'chat-key')

    def test_legacy_model_connections_import_under_generic_names(self):
        source = self.root / 'examples/litellm/.env'
        source.parent.mkdir(parents=True)
        original = 'GEMMA4_API_BASE=http://model.test/v1\nGEMMA4_API_KEY=model-key\nGEMMA4_DEV_API_BASE=http://dev.test/v1\n'
        source.write_text(original)
        destination = self.root / 'import.toml'
        migrate(self.root, destination)
        self.assertEqual(read_service('litellm', destination), {
            'LOCAL_MODEL_API_BASE': 'http://model.test/v1',
            'LOCAL_MODEL_API_KEY': 'model-key',
            'LOCAL_MODEL_DEV_API_BASE': 'http://dev.test/v1',
        })
        self.assertEqual(source.read_text(), original)

    def test_conflicting_legacy_model_connections_leave_no_import(self):
        source = self.root / 'examples/litellm/.env'
        source.parent.mkdir(parents=True)
        source.write_text('GEMMA4_API_KEY=old-private\nLOCAL_MODEL_API_KEY=new-private\n')
        destination = self.root / 'import.toml'
        with self.assertRaises(ConfigurationError):
            migrate(self.root, destination)
        self.assertFalse(destination.exists())


    def test_explicit_exec_loads_file_even_when_implicit_loading_is_disabled(self):
        os.environ['PYTHON_DOTENV_DISABLED'] = '1'
        self.assertEqual(execution_environment('webui')['DATABASE_URL'], 'web-db')
        with self.assertRaises(ConfigurationError):
            execution_environment('typo')


    def test_failed_import_removes_only_its_new_file_and_preserves_sources(self):
        original = self.root / '.env.openwebui'
        original.write_text('DATABASE_URL=original-db\n')
        destination = self.root / 'new.toml'
        with patch('examples.deployment.cli.os.fsync', side_effect=OSError('disk unavailable')), self.assertRaises(OSError):
            migrate(self.root, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(original.read_text(), 'DATABASE_URL=original-db\n')
