"""Historical corrections require exact evidence and preserve billing identity."""
from __future__ import annotations

import hashlib
import json
import socket
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock
from unittest.mock import patch

from examples.ai_usage.config import AiUsageSettings
from examples.ai_usage.deployment.reclassify import apply_or_restore
from examples.ai_usage.deployment.reclassify import context
from examples.ai_usage.deployment.reclassify import fingerprint
from examples.ai_usage.deployment.reclassify import proposed_metadata
from tests.test_ai_usage_service import _settings


class ReclassificationTests(TestCase):
    def setUp(self):
        self.settings: AiUsageSettings = replace(_settings(), official_document_key_hash='d' * 64)
        self.row = {
            'request_id': 'response-exact', 'end_user': 'subject-3', 'spend': 0.12,
            'total_tokens': 120, 'api_key': 'shared-key',
            'metadata': {'keep': 'original', 'spend_logs_metadata': {'keep': 3}},
        }

    def test_company_exact_response_match_preserves_metadata_and_is_idempotent(self):
        evidence = {'response-exact': [('job-a', 'subject-3')]}
        updated, bases = proposed_metadata(self.row, self.settings, {'subject-3'}, evidence, 'run')
        self.assertEqual(bases, ['encrypted_checkpoint_response_id'])
        self.assertEqual(updated['keep'], 'original')
        self.assertEqual(updated['spend_logs_metadata']['keep'], 3)
        self.assertEqual(updated['spend_logs_metadata']['ai_usage_source'], 'company_agent')
        self.assertNotIn('ai_usage_source', self.row['metadata']['spend_logs_metadata'])
        revised = {**self.row, 'metadata': updated}
        self.assertEqual(fingerprint(revised), fingerprint(self.row))
        self.assertEqual(proposed_metadata(revised, self.settings, {'subject-3'}, evidence, 'run-2'), (None, []))

    def test_company_conflicts_are_rejected_without_guessing_owners(self):
        cases = [
            ({**self.row, 'end_user': None}, [('job-a', 'subject-3')]),
            (self.row, [('job-a', 'subject-4')]),
            (self.row, [('job-a', 'subject-3'), ('job-b', 'subject-4')]),
        ]
        for row, references in cases:
            evidence = {'response-exact': references}
            updated, issues = proposed_metadata(
                row, self.settings, {'subject-3'}, evidence, 'run',
            )
            self.assertIsNone(updated)
            self.assertEqual(issues, ['identity_conflict'])
        self.assertEqual(proposed_metadata(self.row, self.settings, {'subject-3'}, {}, 'run'), (None, []))

    def test_document_classification_does_not_create_a_user(self):
        row = {**self.row, 'end_user': None, 'api_key': 'd' * 64}
        updated, bases = proposed_metadata(row, self.settings, {'subject-3'}, {}, 'run')
        tags = updated['spend_logs_metadata']
        self.assertEqual(tags['ai_usage_source'], 'official_document')
        self.assertEqual(tags['ai_usage_classification'], 'document_service')
        self.assertNotIn('ai_usage_subject', tags)
        self.assertEqual(fingerprint({**row, 'metadata': updated}), fingerprint(row))
        self.assertEqual(set(bases), {'dedicated_document_key', 'recorded_accounting_fields'})

    def test_existing_document_subject_cannot_be_activated_by_source_repair(self):
        row = {
            **self.row, 'end_user': None, 'api_key': 'd' * 64,
            'metadata': {'spend_logs_metadata': {'ai_usage_subject': 'subject-3'}},
        }
        result = proposed_metadata(row, self.settings, {'subject-3'}, {}, 'run')
        self.assertEqual(result, (None, ['would_change_owner']))

    def test_fingerprint_detects_billing_and_usage_changes(self):
        for field, value in [('end_user', None), ('spend', 0), ('total_tokens', 0), ('api_key', 'different')]:
            self.assertNotEqual(fingerprint(self.row), fingerprint({**self.row, field: value}))

    def test_context_uses_the_configured_read_only_directory_and_one_source_snapshot(self):
        module = 'examples.ai_usage.deployment.reclassify'
        tenant = '11111111-1111-4111-8111-111111111111'
        settings = replace(
            self.settings, gateway_tenant_id=tenant,
            admin_database_url='postgresql+asyncpg://directory.test/accounts',
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'config.local.toml'
            source.write_text('[webui]\nDATABASE_URL="web-db"\n[litellm]\nLITELLM_DATABASE_URL="ledger-db"\n')
            db = MagicMock()
            db.__enter__.return_value = db
            cursor = db.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [('linked', tenant), ('foreign', 'another-tenant')]
            with patch(module + '.AiUsageSettings.from_environment', return_value=settings), \
                    patch.dict('os.environ', {'AI_GATEWAY_CONFIG': str(source)}), \
                    patch(module + '.psycopg.connect', return_value=db) as connect:
                actual, web, ledger, linked, foreign = context(root)
        self.assertIs(actual, settings)
        self.assertEqual(web, {'DATABASE_URL': 'web-db'})
        self.assertEqual(ledger, {'LITELLM_DATABASE_URL': 'ledger-db'})
        self.assertEqual((linked, foreign), ({'linked'}, {'foreign'}))
        connect.assert_called_once_with(
            'postgresql://directory.test/accounts',
            options='-c default_transaction_read_only=on',
        )
        self.assertEqual(cursor.execute.call_args.args[1], ('keycloak',))

    def test_context_rejects_a_missing_explicit_directory_before_database_access(self):
        module = 'examples.ai_usage.deployment.reclassify'
        settings = replace(self.settings, gateway_tenant_id='11111111-1111-4111-8111-111111111111')
        with patch(module + '.AiUsageSettings.from_environment', return_value=settings), \
                patch(module + '.psycopg.connect') as connect, self.assertRaisesRegex(RuntimeError, 'read-only directory'):
            context(Path('/unused'))
        connect.assert_not_called()

    def test_rollback_preserves_sql_null_and_json_null_distinction(self):
        module = 'examples.ai_usage.deployment.reclassify'
        for sql_null in (False, True):
            with self.subTest(sql_null=sql_null), TemporaryDirectory() as directory:
                run = Path(directory)
                current = {**self.row, 'metadata_sql_null': False}
                item = {
                    'request_id': current['request_id'], 'original_metadata': None,
                    'original_metadata_sql_null': sql_null, 'new_metadata': current['metadata'],
                    'financial_sha256': fingerprint(current),
                }
                plan = json.dumps([item]).encode()
                (run / 'plan.private.json').write_bytes(plan)
                (run / 'applied.json').write_text('{}')
                prepared = {
                    'host': socket.gethostname(), 'plan_sha256': hashlib.sha256(plan).hexdigest(),
                    'database_sha256': hashlib.sha256(b'test-db').hexdigest(),
                }
                (run / 'prepared.json').write_text(json.dumps(prepared))
                db = MagicMock()
                db.__enter__.return_value = db
                db.execute.return_value.fetchone.return_value = current
                with patch(module + '.context', return_value=(None, None, {'LITELLM_DATABASE_URL': 'test-db'}, None, None)), \
                        patch(module + '.psycopg.connect', return_value=db), redirect_stdout(StringIO()):
                    apply_or_restore(run, run, rollback=True)
                update = next(call for call in db.execute.call_args_list if call.args[0].startswith('UPDATE'))
                parameter = update.args[1][0]
                if sql_null:
                    self.assertIsNone(parameter)
                else:
                    self.assertIsNone(parameter.obj)
                self.assertTrue((run / 'rolled-back.json').exists())
