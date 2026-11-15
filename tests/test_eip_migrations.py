"""Real PostgreSQL upgrades in disposable schemas; no EIP/model calls."""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import psycopg
from psycopg import sql

from examples.official_document_workflow.infrastructure.storage import migrations
from examples.official_document_workflow.infrastructure.storage.state import Journal
from tests.eip_postgres import EIPPostgresTestCase


class MigrationTests(EIPPostgresTestCase, unittest.TestCase):
    def connect(self):
        return psycopg.connect(
            self._eip_test_dsn,
            options=f'-c search_path={self._eip_test_schema}',
        )

    def migrate(self):
        migrations.migrate(self._eip_test_dsn, self._eip_test_schema)

    def test_new_install_creates_all_tables_and_journal_is_usable(self):
        journal = Journal()
        with self.connect() as db:
            tables = {
                row[0] for row in db.execute(
                    'SELECT tablename FROM pg_tables WHERE schemaname=%s', (self._eip_test_schema,),
                )
            }
            self.assertEqual(
                tables, {
                    'schema_migrations', 'accounts', 'tasks', 'scan_origins',
                    'task_timeline', 'writes', 'event_receipts', 'source_manifests', 'processing_chunks', 'pending_clarifications', 'clarification_outputs', 'clarification_output_reviews', 'shared_content_cache',
                    'document_analysis_results', 'document_analysis_uses', 'document_analysis_resolutions',
                    'calendar_managed_events', 'calendar_creation_intents',
                },
            )
            self.assertEqual(
                db.execute('SELECT version FROM schema_migrations ORDER BY version').fetchall(), [
                    (n,) for n in range(2, migrations.VERSION + 1)
                ],
            )
        self.assertEqual(journal.reserve_event('event'), 'reserved')
        journal.done('event', receipt={'event_id': 'remote-id', 'detail_verified': True})
        self.assertEqual(Journal().reserve_event('event'), 'done')

    def test_unversioned_upgrade_preserves_data_and_only_backfills_unknown_time(self):
        with self.connect() as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self._eip_test_schema)))
            db.execute("""
                CREATE TABLE accounts (owner TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    secret BYTEA, generation TEXT NOT NULL, enabled INTEGER NOT NULL,
                    next_run DOUBLE PRECISION NOT NULL, last_run DOUBLE PRECISION,
                    message TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE tasks (id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    identity TEXT NOT NULL, document_id TEXT NOT NULL, phase TEXT NOT NULL,
                    payload BYTEA NOT NULL, updated DOUBLE PRECISION NOT NULL,
                    UNIQUE(owner,identity,document_id));
                CREATE TABLE writes (key TEXT PRIMARY KEY, status TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
                CREATE TABLE event_receipts (key TEXT PRIMARY KEY, event_id TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
                CREATE TABLE task_timeline (task_id TEXT PRIMARY KEY, first_detected_at REAL, last_processed_at REAL);
                INSERT INTO accounts VALUES ('alice','17','encrypted-secret','generation',1,100,50,'ready',2);
                INSERT INTO accounts VALUES ('bob','18','other-secret','other-generation',0,200,50,'paused',0);
                INSERT INTO tasks VALUES ('old','alice','17','doc','needs_review','encrypted-payload',123);
                INSERT INTO tasks VALUES ('known','alice','17','doc2','done','other-payload',456);
                INSERT INTO task_timeline VALUES ('known',10,20);
                INSERT INTO writes VALUES ('done','done',10),('uncertain','unknown',11);
                INSERT INTO event_receipts VALUES ('done','remote-id',10);
            """)
        self.migrate()
        with self.connect() as db:
            self.assertEqual(
                db.execute('SELECT owner,secret,enabled,next_run,generation FROM accounts ORDER BY owner').fetchall(),
                [
                    ('alice', b'encrypted-secret', True, 100, 'generation'),
                    ('bob', b'other-secret', False, 200, 'other-generation'),
                ],
            )
            self.assertEqual(
                db.execute('SELECT payload,phase,updated,retry_at FROM tasks WHERE id=%s', ('old',)).fetchone(),
                (b'encrypted-payload', 'needs_review', 123, 0),
            )
            self.assertEqual(
                db.execute('SELECT * FROM task_timeline ORDER BY task_id').fetchall(),
                [('known', 10, 20), ('old', None, None)],
            )
            self.assertEqual(
                db.execute('SELECT key,status FROM writes ORDER BY key').fetchall(),
                [('done', 'done'), ('uncertain', 'unknown')],
            )
            self.assertEqual(db.execute('SELECT event_id FROM event_receipts').fetchone()[0], 'remote-id')
            self.assertEqual(
                db.execute(
                    'SELECT calendar_attach_renamed_pdf FROM accounts WHERE owner=%s', ('alice',),
                ).fetchone(), (False,),
            )

    def test_restart_skips_upgrade_and_preserves_version_timestamp(self):
        self.migrate()
        with self.connect() as db:
            before = db.execute('SELECT * FROM schema_migrations').fetchall()
        with patch.object(migrations, '_initial_schema', side_effect=AssertionError('upgrade repeated')):
            self.migrate()
            Journal()
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT * FROM schema_migrations').fetchall(), before)

    def test_failed_upgrade_rolls_back_and_can_retry(self):
        initial = migrations._initial_schema

        def fail(db, schema):
            initial(db, schema)
            raise RuntimeError('injected migration failure')
        with patch.object(migrations, '_initial_schema', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.migrate()
        with self.connect() as db:
            self.assertIsNone(db.execute('SELECT to_regclass(%s)', (self._eip_test_schema+'.accounts',)).fetchone()[0])
            self.assertIsNone(
                db.execute(
                    'SELECT to_regclass(%s)',
                    (self._eip_test_schema+'.schema_migrations',),
                ).fetchone()[0],
            )
        self.migrate()

    def test_concurrent_first_start_runs_one_upgrade(self):
        start = Barrier(2)

        def worker():
            start.wait(timeout=5)
            self.migrate()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for future in futures:
                future.result(timeout=15)
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], migrations.VERSION - 1)

    def test_newer_database_is_not_downgraded(self):
        self.migrate()
        with self.connect() as db:
            db.execute('INSERT INTO schema_migrations(version) VALUES (99)')
        with self.assertRaises(ValueError):
            self.migrate()
        with self.connect() as db:
            self.assertEqual(
                db.execute(
                    'SELECT version FROM schema_migrations ORDER BY version',
                ).fetchall(), [(n,) for n in range(2, migrations.VERSION + 1)] + [(99,)],
            )

    def test_version_three_upgrade_preserves_existing_receipts(self):
        self.migrate()
        with self.connect() as db:
            db.execute("INSERT INTO writes VALUES ('existing-write','unknown',12)")
            db.execute('DROP TABLE pending_clarifications')
            db.execute('DELETE FROM schema_migrations WHERE version>=4')
        self.migrate()
        with self.connect() as db:
            self.assertEqual(db.execute('SELECT key,status FROM writes').fetchall(), [('existing-write', 'unknown')])
            self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], migrations.VERSION)
            self.assertEqual(db.execute('SELECT count(*) FROM pending_clarifications').fetchone()[0], 0)

    def test_version_nine_upgrade_only_adds_shared_content_and_preserves_private_bytes(self):
        self.migrate()
        with self.connect() as db:
            db.execute('DROP TABLE shared_content_cache')
            db.execute('DELETE FROM schema_migrations WHERE version>=10')
            db.execute("INSERT INTO processing_chunks VALUES('owner','private',%s,123)", (b'original-ciphertext',))
            db.execute("INSERT INTO writes VALUES('uncertain','unknown',456)")
            original = db.execute('SELECT * FROM schema_migrations WHERE version<10 ORDER BY version').fetchall()
        with patch.object(migrations, '_version_nine', side_effect=AssertionError('v9 migration repeated')):
            self.migrate()
        with self.connect() as db:
            self.assertEqual(
                db.execute('SELECT * FROM processing_chunks').fetchall(),
                [('owner', 'private', b'original-ciphertext', 123)],
            )
            self.assertEqual(db.execute('SELECT * FROM writes').fetchall(), [('uncertain', 'unknown', 456)])
            self.assertEqual(
                db.execute(
                    'SELECT * FROM schema_migrations WHERE version<10 ORDER BY version',
                ).fetchall(), original,
            )
            self.assertIsNotNone(db.execute("SELECT to_regclass('shared_content_cache')").fetchone()[0])

    def test_version_eleven_upgrade_adds_calendar_provenance_without_rewriting_private_data(self):
        self.migrate()
        with self.connect() as db:
            db.execute('DROP TABLE calendar_managed_events,calendar_creation_intents')
            db.execute('DELETE FROM schema_migrations WHERE version=12')
            db.execute("INSERT INTO processing_chunks VALUES('owner','private',%s,123)", (b'original-ciphertext',))
            db.execute("INSERT INTO writes VALUES('calendar-done','done',456),('calendar-unknown','unknown',789)")
            db.execute("INSERT INTO event_receipts VALUES('calendar-done','original-event',456)")
            original = db.execute('SELECT * FROM schema_migrations ORDER BY version').fetchall()
        self.migrate()
        with self.connect() as db:
            self.assertEqual(
                db.execute('SELECT * FROM processing_chunks').fetchall(),
                [('owner', 'private', b'original-ciphertext', 123)],
            )
            self.assertEqual(
                db.execute('SELECT key,status FROM writes ORDER BY key').fetchall(),
                [('calendar-done', 'done'), ('calendar-unknown', 'unknown')],
            )
            self.assertEqual(
                db.execute('SELECT * FROM event_receipts').fetchall(),
                [('calendar-done', 'original-event', 456)],
            )
            self.assertEqual(
                db.execute(
                    'SELECT * FROM schema_migrations WHERE version<12 ORDER BY version',
                ).fetchall(), original,
            )
            self.assertEqual(db.execute('SELECT count(*) FROM calendar_managed_events').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM calendar_creation_intents').fetchone()[0], 0)
