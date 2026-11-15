"""Transactional, versioned EIP schema upgrades shared by all storage clients."""
from __future__ import annotations

import psycopg
from psycopg import sql

VERSION = 12


def migrate(dsn: str, schema: str) -> None:
    # The transaction lock also covers first installation, before any table exists.
    # A failed upgrade rolls back both its DDL and its version marker.
    with psycopg.connect(
        dsn,
        connect_timeout=5,
        options='-c lock_timeout=5000 -c statement_timeout=30000',
    ) as db:
        db.execute(
            'SELECT pg_advisory_xact_lock(hashtext(%s))',
            ('examples.official_document_workflow:' + schema,),
        )
        db.execute(
            sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)),
        )
        exists = db.execute(
            'SELECT to_regclass(%s)', (f'{schema}.schema_migrations',),
        ).fetchone()[0]
        version = 0
        if exists:
            version = db.execute(
                'SELECT COALESCE(MAX(version),0) FROM schema_migrations',
            ).fetchone()[0]
            if version > VERSION:
                raise ValueError('EIP 資料庫版本較新，請更新服務後再啟動。')
            if version == VERSION:
                return
        db.execute(
            sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)),
        )
        db.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)',
        )
        if version < 2:
            _initial_schema(db, schema)
            db.execute('INSERT INTO schema_migrations(version) VALUES (2)')
        if version < 3:
            db.execute(
                'CREATE INDEX IF NOT EXISTS processing_chunks_expiration ON processing_chunks(updated,owner,key)',
            )
            db.execute('INSERT INTO schema_migrations(version) VALUES (3)')
        if version < 4:
            db.execute("""
                CREATE TABLE IF NOT EXISTS pending_clarifications (
                    owner TEXT NOT NULL, chat TEXT NOT NULL,
                    generation BIGSERIAL UNIQUE NOT NULL,
                    fingerprint TEXT NOT NULL, payload BYTEA NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
                    claimed BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY(owner,chat));
                CREATE INDEX IF NOT EXISTS pending_clarifications_expiration ON pending_clarifications(expires_at);
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES (4)')
        if version < 5:
            db.execute("""
                CREATE TABLE IF NOT EXISTS clarification_outputs (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('writing','not_written','unknown','stored')),
                    payload BYTEA NOT NULL, updated DOUBLE PRECISION NOT NULL);
                CREATE INDEX IF NOT EXISTS clarification_outputs_owner ON clarification_outputs(owner,updated);
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(5)')
        if version < 6:
            db.execute("""
                CREATE TABLE IF NOT EXISTS clarification_output_reviews (
                    id BIGSERIAL PRIMARY KEY, output_id TEXT NOT NULL REFERENCES clarification_outputs(id),
                    owner TEXT NOT NULL, actor TEXT NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('manual','webui_sha256')),
                    outcome TEXT NOT NULL, payload BYTEA NOT NULL, created DOUBLE PRECISION NOT NULL);
                CREATE INDEX IF NOT EXISTS clarification_reviews_owner ON clarification_output_reviews(owner,output_id,id DESC);
                CREATE INDEX IF NOT EXISTS clarification_outputs_review_owner ON clarification_outputs(owner,state,updated DESC,id DESC);
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(6)')
        if version < 7:
            db.execute("""
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS job_id TEXT;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'calendar_csv_pending_reminder';
                ALTER TABLE clarification_outputs DROP CONSTRAINT IF EXISTS clarification_outputs_state_check;
                ALTER TABLE clarification_outputs ADD CONSTRAINT clarification_outputs_state_check
                    CHECK(state IN ('writing','not_written','unknown','stored','deleted'));
                CREATE INDEX IF NOT EXISTS clarification_outputs_job ON clarification_outputs(owner,job_id) WHERE job_id IS NOT NULL;
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(7)')
        if version < 8:
            db.execute("""
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 1;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS next_review DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_until DOUBLE PRECISION NOT NULL DEFAULT 0;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_token TEXT;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_failures INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS projection_pending BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE clarification_outputs DROP CONSTRAINT IF EXISTS clarification_outputs_state_check;
                ALTER TABLE clarification_outputs ADD CONSTRAINT clarification_outputs_state_check
                    CHECK(state IN ('writing','not_written','unknown','stored','deleting','delete_unknown','deleted'));
                UPDATE clarification_outputs SET projection_pending=TRUE
                    WHERE job_id IS NOT NULL AND state IN ('stored','deleted');
                CREATE INDEX IF NOT EXISTS clarification_outputs_review_due
                    ON clarification_outputs(next_review,updated,id)
                    WHERE state IN ('writing','unknown','deleting','delete_unknown') OR projection_pending;
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(8)')
        if version < 9:
            _version_nine(db)
            db.execute('INSERT INTO schema_migrations(version) VALUES(9)')
        if version < 10:
            db.execute("""
                CREATE TABLE IF NOT EXISTS shared_content_cache (
                    scope TEXT NOT NULL, key TEXT NOT NULL, revision TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ready','failed','unknown')), payload BYTEA NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE INDEX IF NOT EXISTS shared_content_cache_expiration ON shared_content_cache(expires_at,scope,key);
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(10)')
        if version < 11:
            db.execute("""
                CREATE TABLE IF NOT EXISTS document_analysis_results (
                    scope TEXT NOT NULL, key TEXT NOT NULL, revision TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ready','failed','unknown')), payload BYTEA NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE INDEX IF NOT EXISTS document_analysis_created ON document_analysis_results(created_at DESC);
                CREATE INDEX IF NOT EXISTS document_analysis_state ON document_analysis_results(state,created_at DESC);
                CREATE TABLE IF NOT EXISTS document_analysis_uses (
                    scope TEXT NOT NULL, key TEXT NOT NULL, owner TEXT NOT NULL, job TEXT NOT NULL,
                    first_used_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(scope,key,owner,job),
                    FOREIGN KEY(scope,key) REFERENCES document_analysis_results(scope,key));
                CREATE TABLE IF NOT EXISTS document_analysis_resolutions (
                    id BIGSERIAL PRIMARY KEY, scope TEXT NOT NULL, key TEXT NOT NULL,
                    actor TEXT NOT NULL, reason TEXT NOT NULL, previous_payload_sha256 TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(scope,key) REFERENCES document_analysis_results(scope,key));
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(11)')
        if version < 12:
            db.execute("""
                CREATE TABLE IF NOT EXISTS calendar_managed_events (
                    identity TEXT NOT NULL, event_id TEXT NOT NULL, source_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('ready','review','deleting','deleted')),
                    payload BYTEA NOT NULL, backup BYTEA,
                    next_check DOUBLE PRECISION NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(identity,event_id));
                CREATE INDEX IF NOT EXISTS calendar_managed_due ON calendar_managed_events(identity,next_check,event_id)
                    WHERE state='ready';
                CREATE TABLE IF NOT EXISTS calendar_creation_intents (
                    identity TEXT NOT NULL, source_key TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('unknown','created')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(identity,source_key));
            """)
            db.execute('INSERT INTO schema_migrations(version) VALUES(12)')


def _version_nine(db):
    db.execute("""
            ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_required BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_outcome TEXT;
            ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_reason TEXT;
            ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS review_evidence BYTEA;
            ALTER TABLE clarification_outputs ADD COLUMN IF NOT EXISTS mismatch_count INTEGER NOT NULL DEFAULT 0;
            DROP INDEX IF EXISTS clarification_outputs_review_due;
            CREATE INDEX clarification_outputs_review_due ON clarification_outputs(next_review,updated,id)
                WHERE (NOT review_required AND state IN ('writing','unknown','deleting','delete_unknown')) OR projection_pending;
        """)


def _initial_schema(db, schema):
    # Adopt unversioned installations without replacing ciphertext or receipts.
    db.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
          owner TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, secret BYTEA,
          generation TEXT NOT NULL, enabled BOOLEAN NOT NULL,
          next_run DOUBLE PRECISION NOT NULL, last_run DOUBLE PRECISION,
          message TEXT NOT NULL DEFAULT '', failures INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tasks (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, identity TEXT NOT NULL,
          document_id TEXT NOT NULL, phase TEXT NOT NULL, payload BYTEA NOT NULL,
          updated DOUBLE PRECISION NOT NULL, UNIQUE(owner,identity,document_id));
        CREATE TABLE IF NOT EXISTS scan_origins (
          owner TEXT NOT NULL, identity TEXT NOT NULL, scan_start DOUBLE PRECISION NOT NULL,
          PRIMARY KEY(owner,identity));
        CREATE INDEX IF NOT EXISTS tasks_owner_history ON tasks(owner,updated DESC,id DESC);
        CREATE TABLE IF NOT EXISTS task_timeline (
          task_id TEXT PRIMARY KEY, first_detected_at REAL, last_processed_at REAL);
        CREATE TABLE IF NOT EXISTS writes (
          key TEXT PRIMARY KEY, status TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
        CREATE TABLE IF NOT EXISTS event_receipts (
          key TEXT PRIMARY KEY, event_id TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL);
        CREATE TABLE IF NOT EXISTS source_manifests (key TEXT PRIMARY KEY, manifest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS processing_chunks (
          owner TEXT NOT NULL, key TEXT NOT NULL, payload BYTEA NOT NULL,
          updated DOUBLE PRECISION NOT NULL, PRIMARY KEY(owner,key));
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS scan_start DOUBLE PRECISION;
        ALTER TABLE tasks ADD COLUMN IF NOT EXISTS retry_at DOUBLE PRECISION NOT NULL DEFAULT 0;
        ALTER TABLE accounts ADD COLUMN IF NOT EXISTS calendar_attach_renamed_pdf BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE accounts DROP COLUMN IF EXISTS calendar_project_member_sharing;
    """)
    enabled_type = db.execute(
        """SELECT data_type FROM information_schema.columns
        WHERE table_schema=%s AND table_name='accounts' AND column_name='enabled'""",
        (schema,),
    ).fetchone()[0]
    if enabled_type != 'boolean':
        db.execute(
            "ALTER TABLE accounts ALTER COLUMN enabled TYPE BOOLEAN USING CASE WHEN enabled::text IN ('1','t','true') THEN TRUE ELSE FALSE END",
        )
    # Old tasks have no provable first-observation time; preserve unknown as NULL.
    db.execute(
        'INSERT INTO task_timeline(task_id) SELECT id FROM tasks ON CONFLICT DO NOTHING',
    )
