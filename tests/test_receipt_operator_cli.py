"""Operator queries require existing state and cannot reopen an uncertain writer."""
from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from examples.official_document_workflow.cli.output_receipts import _origin
from examples.official_document_workflow.cli.output_receipts import _token
from examples.official_document_workflow.cli.output_receipts import execute
from examples.official_document_workflow.cli.output_receipts import existing_store
from examples.official_document_workflow.infrastructure.storage.automation_store import AutomationStore
from examples.official_document_workflow.infrastructure.storage.output_receipts import PostgresOutputReceipts
from tests.eip_postgres import EIPPostgresTestCase


class ReceiptOperatorTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.key = Path(temp.name) / 'key'
        self.backend = AutomationStore(self.key)
        self.repo = PostgresOutputReceipts(lambda: self.backend)
        record, _ = self.repo.begin('output-id', 'owner', 'private.csv', 'hash', 'source')
        self.repo.mark(record, 'unknown')
        self.enterContext(patch.dict('os.environ', {'EIP_AUTOMATION_KEY_FILE': str(self.key)}))

    async def test_read_and_annotate_are_owner_scoped_and_preserve_uncertain_writes(self):
        key_bytes = self.key.read_bytes()
        with self.backend.db() as db:
            versions = db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        result = await execute(argparse.Namespace(command='list', owner='owner', state='unknown', limit=20))
        self.assertEqual([r['id'] for r in result], ['output-id'])
        self.assertNotIn('private.csv', str(result))
        self.assertEqual(await execute(argparse.Namespace(command='list', owner='other', state=None, limit=20)), [])
        inspected = await execute(argparse.Namespace(command='inspect', owner='owner', output_id='output-id'))
        self.assertEqual(inspected['receipt']['state'], 'unknown')
        await execute(
            argparse.Namespace(
                command='annotate', owner='owner', output_id='output-id',
                actor='local-operator', outcome='investigating', note='Checked the download inventory.',
            ),
        )
        self.assertEqual(self.repo.get('output-id', 'owner').state, 'unknown')
        self.assertFalse(self.repo.begin('output-id', 'owner', 'x.csv', 'x', 'source')[1])
        self.assertEqual(self.key.read_bytes(), key_bytes)
        with self.backend.db() as db:
            self.assertEqual(db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall(), versions)

    async def test_operator_read_does_not_initialize_or_upgrade_a_database(self):
        with self.backend.db() as db:
            db.execute('DROP TABLE clarification_output_reviews')
            db.execute('DELETE FROM schema_migrations WHERE version>=6')
        with self.assertRaises(ValueError):
            existing_store()
        with self.backend.db() as db:
            self.assertEqual(db.execute('SELECT max(version) AS n FROM schema_migrations').fetchone()['n'], 5)
        AutomationStore(self.key)  # Only normal service initialization applies the upgrade.
        self.assertEqual(self.repo.get('output-id', 'owner').state, 'unknown')
        with patch.dict('os.environ', {'EIP_AUTOMATION_KEY_FILE': str(self.key.parent/'missing')}):
            with self.assertRaises(ValueError):
                existing_store()
        self.assertFalse((self.key.parent/'missing').exists())


class TokenAndOriginTests(unittest.TestCase):
    def test_token_file_must_be_private_and_is_never_taken_from_a_url(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp)/'token'
            token.write_text('example-bearer\n')
            token.chmod(0o600)
            self.assertEqual(_token(token), 'example-bearer')
            token.chmod(0o644)
            with self.assertRaises(ValueError):
                _token(token)
        for origin in ('https://remote.test', 'http://127.0.0.1?', 'http://user@127.0.0.1', 'http://127.0.0.1/path'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                _origin(origin)
        self.assertEqual(_origin('http://127.0.0.1:18082'), 'http://127.0.0.1:18082')
