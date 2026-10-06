"""Transactional, versioned EIP schema upgrades shared by all storage clients."""
from __future__ import annotations

import psycopg
from psycopg import sql


VERSION = 2


def migrate(dsn: str, schema: str) -> None:
    # The transaction lock also covers first installation, before any table exists.
    # A failed upgrade rolls back both its DDL and its version marker.
    with psycopg.connect(
        dsn, connect_timeout=5,
        options='-c lock_timeout=5000 -c statement_timeout=30000',
    ) as db:
        db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('official_document_workflow:' + schema,))
        db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        exists = db.execute('SELECT to_regclass(%s)', (f'{schema}.schema_migrations',)).fetchone()[0]
        if exists:
            version = db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
            if version > VERSION:
                raise ValueError('EIP 資料庫版本較新，請更新服務後再啟動。')
            if version == VERSION:
                return
        db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
        db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        _initial_schema(db, schema)
        db.execute('INSERT INTO schema_migrations(version) VALUES (%s)', (VERSION,))


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
        WHERE table_schema=%s AND table_name='accounts' AND column_name='enabled'""", (schema,),
    ).fetchone()[0]
    if enabled_type != 'boolean':
        db.execute("ALTER TABLE accounts ALTER COLUMN enabled TYPE BOOLEAN USING CASE WHEN enabled::text IN ('1','t','true') THEN TRUE ELSE FALSE END")
    # Old tasks have no provable first-observation time; preserve unknown as NULL.
    db.execute('INSERT INTO task_timeline(task_id) SELECT id FROM tasks ON CONFLICT DO NOTHING')
