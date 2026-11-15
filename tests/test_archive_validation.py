"""A clean checkout validates metadata; explicit archive audits remain strict."""

from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from examples.deployment.tools import verify_archive as archive
from examples.deployment.tools import validate as validation


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.index = self.root / 'integrity.json'
        self.name = 'examples/deployment/archive/releases/sample.zip'
        self.payload = b'original release bytes'
        self.index.write_text(
            json.dumps({self.name: hashlib.sha256(self.payload).hexdigest()})
        )
        self.enterContext(patch.object(archive, 'INDEX', self.index))
        self.location = self.root / 'external-archive'

    def restore(self):
        path = self.location / 'releases/sample.zip'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.payload)
        return path

    def test_manifest_validates_without_any_archive_directory(self):
        with patch.object(
            archive.hashlib,
            'file_digest',
            side_effect=AssertionError('must not read archive bytes'),
        ):
            self.assertEqual(archive.verify_manifest(), 1)
        self.assertFalse(self.location.exists())

    def test_explicit_audit_requires_files_and_accepts_an_external_restore(self):
        with self.assertRaisesRegex(ValueError, 'missing'):
            archive.verify(self.location)
        self.restore()
        self.assertEqual(archive.verify(self.location), 1)

    def test_modified_release_and_symlink_are_not_accepted(self):
        path = self.restore()
        path.write_bytes(b'modified release')
        with self.assertRaisesRegex(ValueError, 'integrity'):
            archive.verify(self.location)
        path.unlink()
        external = self.root / 'elsewhere.zip'
        external.write_bytes(self.payload)
        path.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            archive.verify(self.location)

    def test_metadata_rejects_invalid_paths_and_hashes_without_files(self):
        for name, sha in (
            ('../../private', '0' * 64),
            ('/examples/deployment/archive/releases/file.zip', '0' * 64),
            ('examples/deployment/archive/releases/../file.zip', '0' * 64),
            ('examples/deployment/archive/integrity.json', '0' * 64),
            ('examples/deployment/archive/releases/file.zip', 'not-a-digest'),
        ):
            with self.subTest(name=name, sha=sha):
                self.index.write_text(json.dumps({name: sha}))
                with self.assertRaises(ValueError):
                    archive.verify_manifest()

    def test_cli_distinguishes_missing_local_bytes_from_metadata_success(self):
        package = self.root / 'checkout/examples/deployment'
        package.mkdir(parents=True)
        (package.parent / '__init__.py').touch()
        (package / '__init__.py').touch()
        (package / 'tools').mkdir()
        (package / 'tools/__init__.py').touch()
        (package / 'tools/verify_archive.py').write_bytes(Path(archive.__file__).read_bytes())
        (package / 'archive').mkdir()
        (package / 'archive/integrity.json').write_bytes(self.index.read_bytes())
        command = [sys.executable, '-m', 'examples.deployment.tools.verify_archive']

        def run(*args):
            return subprocess.run(
                [*command, *args],
                cwd=package.parents[1],
                capture_output=True,
                text=True,
            )

        checked = run('--manifest-only')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('bytes were not checked', checked.stdout)
        self.assertNotEqual(run().returncode, 0)
        self.restore()
        checked = run('--archive-dir', str(self.location))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn('Verified 1', checked.stdout)


class ValidationArchiveSelectionTests(unittest.TestCase):
    def check(self, *options):
        class ChecksFinished(Exception):
            pass

        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            output = Path(temporary) / 'validation'
            stack.enter_context(
                patch.object(
                    sys, 'argv', ['validate', '--output', str(output), *options]
                )
            )
            execute = stack.enter_context(patch.object(validation, 'execute'))
            stack.enter_context(
                patch.object(validation, 'postgres', side_effect=ChecksFinished)
            )
            with self.assertRaises(ChecksFinished):
                validation.main()
            commands = [
                call.args[0]
                for call in execute.call_args_list
                if 'examples.deployment.tools.verify_archive' in call.args[0]
            ]
            report = json.loads((output / 'report.json').read_text())
        return commands, report

    def test_default_reports_byte_audit_as_skipped(self):
        commands, report = self.check()
        self.assertEqual(len(commands), 1)
        self.assertIn('--manifest-only', commands[0])
        audit = next(p for p in report['phases'] if p['name'] == 'archive integrity')
        self.assertEqual(audit['status'], 'skipped')
        self.assertIn('--with-archive', audit['reason'])

    def test_archive_flag_enables_strict_local_audit(self):
        commands, report = self.check('--with-archive')
        self.assertEqual(len(commands), 2)
        self.assertNotIn('--manifest-only', commands[1])
        self.assertEqual(report['phases'][-1]['status'], 'passed')

    def test_external_location_implies_strict_audit(self):
        commands, _ = self.check('--archive-dir', '/private/releases-backup')
        self.assertEqual(
            commands[1][-2:], ['--archive-dir', '/private/releases-backup']
        )
