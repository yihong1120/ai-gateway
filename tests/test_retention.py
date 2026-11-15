"""Cache cleanup preserves active analysis and all external-write receipts."""
from __future__ import annotations

import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from .eip_postgres import EIPPostgresTestCase
from examples.official_document_workflow.infrastructure.storage.processing_cache import (
    ProcessingCache,
)
from examples.official_document_workflow.infrastructure.storage.state import Journal
from examples.shared.runtime import retention
from examples.shared.runtime.retention import Retention


class RetentionTests(EIPPostgresTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_bounded_cleanup_preserve_protected_and_active_rows(self):
        journal = Journal()
        cutoff = time.time() - 2 * 86400
        with journal.connect() as db:
            for key, updated in [('expired', 0), ('active', 0), ('fresh', time.time())]:
                db.execute(
                    'INSERT INTO processing_chunks VALUES(%s,%s,%s,%s)',
                    ('bob' if key == 'expired' else 'alice', key, b'ciphertext', updated),
                )
            db.execute("INSERT INTO writes VALUES('unknown-operation','unknown',0)")
            db.execute('CREATE TABLE custom_history(value TEXT)')
            db.execute("INSERT INTO custom_history VALUES('preserved')")
        policy = Retention(journal.dsn, journal.schema)
        self.assertEqual(policy.expired_cache(cutoff)['rows'], 2)
        backend = ProcessingCache(SimpleNamespace(dsn=journal.dsn, schema=journal.schema), 'alice')
        async with backend.lock('an-entire-upload-batch'):
            self.assertEqual(policy.expired_cache(cutoff, apply=True)['rows'], 1)
        with journal.connect() as db:
            self.assertEqual(
                db.execute('SELECT key FROM processing_chunks ORDER BY key').fetchall(), [('active',), ('fresh',)],
            )
            self.assertEqual(db.execute('SELECT status FROM writes').fetchone(), ('unknown',))
        report = {row['table']: row for row in policy.report(exact_tables=['custom_history'])}
        self.assertEqual(report['writes']['category'], 'external_write_receipt')
        self.assertFalse(report['writes']['automatic_deletion'])
        self.assertEqual(report['custom_history']['category'], 'protected_unclassified')
        self.assertEqual(report['custom_history']['rows'], 1)

    def test_cleanup_rejects_recent_cutoff_and_unbounded_batches(self):
        journal = Journal()
        policy = Retention(journal.dsn, journal.schema)
        for cutoff, limit in [(time.time(), 100), (0, 0), (0, 1001), (float('nan'), 100), (float('inf'), 100), (0, True)]:
            with self.assertRaises(ValueError):
                policy.expired_cache(cutoff, limit=limit, apply=True)

    def test_default_inventory_uses_catalog_estimates_and_exact_counts_are_opt_in(self):
        journal = Journal()
        with journal.connect() as db:
            db.execute('CREATE TABLE inventory_probe(value INTEGER)')
            db.execute('INSERT INTO inventory_probe VALUES(1),(2),(3)')
            db.execute('ANALYZE inventory_probe')
        policy = Retention(journal.dsn, journal.schema)
        original, queries = retention.connection, []

        @contextmanager
        def observed(*args, **kwargs):
            with original(*args, **kwargs) as db:
                proxy = Mock(wraps=db)
                yield proxy
                queries.extend(str(call.args[0]).lower() for call in proxy.execute.call_args_list)
        with patch.object(retention, 'connection', observed):
            estimated = {row['table']: row for row in policy.report()}
        self.assertFalse(any('count(' in query for query in queries))
        self.assertEqual(estimated['inventory_probe']['rows_kind'], 'estimate')
        self.assertEqual(estimated['inventory_probe']['rows'], 3)
        exact = {row['table']: row for row in policy.report(exact_tables=['inventory_probe'])}
        self.assertEqual(exact['inventory_probe']['rows_kind'], 'exact')
        self.assertEqual(exact['inventory_probe']['rows'], 3)
        with self.assertRaises(ValueError):
            policy.report(exact_tables=['outside_schema'])
