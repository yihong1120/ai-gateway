"""Versioned, transactional company-agent schema upgrades, including legacy adoption."""

from __future__ import annotations

import psycopg
from psycopg import sql

VERSION = 6


def migrate(conninfo, schema):
    with psycopg.connect(conninfo) as db:
        db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('company_agent:schema:' + schema,))
        db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(schema)))
        db.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(schema)))
        db.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)'
        )
        version = db.execute('SELECT COALESCE(MAX(version),0) FROM schema_migrations').fetchone()[0]
        if version > VERSION:
            raise ValueError('Company agent database is newer than this application.')
        for number, apply in enumerate((_initial, _chat_outbox, _delivery_retries, _job_failures, _multiple_nas, _company_mcp), 1):
            if number > version:
                apply(db)
                db.execute('INSERT INTO schema_migrations(version) VALUES(%s)', (number,))


def _company_mcp(db):
    db.execute('''
        CREATE TABLE mcp_grants (
            owner TEXT NOT NULL,client TEXT NOT NULL,enabled BOOLEAN NOT NULL,
            revision TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,PRIMARY KEY(owner,client));
        CREATE TABLE mcp_workspaces (
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,subject TEXT NOT NULL,client TEXT NOT NULL,
            grant_revision TEXT NOT NULL,snapshot TEXT NOT NULL,expires DOUBLE PRECISION NOT NULL);
        CREATE INDEX mcp_workspaces_owner ON mcp_workspaces(owner,expires);
        CREATE TABLE mcp_operations (
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,client TEXT NOT NULL,workspace TEXT NOT NULL,
            request_id TEXT NOT NULL,digest TEXT NOT NULL,payload TEXT NOT NULL,
            web_job TEXT,created DOUBLE PRECISION NOT NULL,UNIQUE(owner,client,request_id));
        CREATE TABLE mcp_request_limits (
            owner TEXT PRIMARY KEY,window_start BIGINT NOT NULL,attempts INTEGER NOT NULL);
    ''', prepare=False)


def _multiple_nas(db):
    # Preserve ciphertext and revision: legacy checkpoints still address the same account.
    db.execute('''
        ALTER TABLE nas_bindings ADD COLUMN binding_id TEXT;
        UPDATE nas_bindings SET binding_id=revision;
        ALTER TABLE nas_bindings ALTER COLUMN binding_id SET NOT NULL;
        ALTER TABLE nas_bindings DROP CONSTRAINT nas_bindings_pkey;
        ALTER TABLE nas_bindings ADD PRIMARY KEY(owner,binding_id);
        CREATE UNIQUE INDEX nas_bindings_owner_revision ON nas_bindings(owner,revision);
    ''')


def _initial(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT,status TEXT,result TEXT,created DOUBLE PRECISION);
                    ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delivery_finalized BOOLEAN NOT NULL DEFAULT FALSE;
                    CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,job TEXT,owner TEXT,name TEXT,sha TEXT,data BYTEA);
                    CREATE INDEX IF NOT EXISTS artifacts_owner_job ON artifacts(owner,job);
                    ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS lifecycle TEXT NOT NULL DEFAULT 'draft';
                    ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS superseded_by TEXT;
                    CREATE TABLE IF NOT EXISTS analysis_evidence(owner TEXT,job TEXT,id TEXT,payload TEXT NOT NULL,
                      PRIMARY KEY(owner,job,id));
                    CREATE TABLE IF NOT EXISTS artifact_validations(owner TEXT,job TEXT,artifact TEXT,sha TEXT,payload TEXT NOT NULL,
                      PRIMARY KEY(owner,job,artifact,sha));
                    CREATE TABLE IF NOT EXISTS delivery_plans(owner TEXT,job TEXT,payload TEXT NOT NULL,
                      PRIMARY KEY(owner,job));
                    CREATE TABLE IF NOT EXISTS assignment_claims(sequence BIGSERIAL PRIMARY KEY,
                      owner TEXT NOT NULL,job TEXT NOT NULL,work_key TEXT NOT NULL,call_id TEXT NOT NULL,
                      payload TEXT NOT NULL,UNIQUE(owner,job,work_key),UNIQUE(owner,job,call_id));
                    CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,job TEXT,owner TEXT,artifact TEXT,root TEXT,path TEXT,status TEXT);
                    CREATE TABLE IF NOT EXISTS audit(job TEXT,owner TEXT,action TEXT,status TEXT,created DOUBLE PRECISION);
                    CREATE TABLE IF NOT EXISTS model_attempts(job TEXT PRIMARY KEY,attempts INTEGER);
                    CREATE TABLE IF NOT EXISTS job_budgets(job TEXT PRIMARY KEY,owner TEXT NOT NULL,step_limit INTEGER NOT NULL CHECK(step_limit BETWEEN 1 AND 80));
                    CREATE TABLE IF NOT EXISTS eip_calendar_sources(owner TEXT,job TEXT,artifact TEXT,revision TEXT,
                        csv_format TEXT NOT NULL DEFAULT 'plain',
                        PRIMARY KEY(owner,job,artifact));
                    ALTER TABLE eip_calendar_sources ADD COLUMN IF NOT EXISTS csv_format TEXT NOT NULL DEFAULT 'plain';
                    CREATE TABLE IF NOT EXISTS tool_receipts(owner TEXT,job TEXT,call_id TEXT,name TEXT,args_hash TEXT,result TEXT,
                      PRIMARY KEY(owner,job,call_id));
                    CREATE TABLE IF NOT EXISTS eip_sources(owner TEXT NOT NULL,job TEXT NOT NULL,handle TEXT NOT NULL,
                      revision TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(owner,job,handle));
                    CREATE TABLE IF NOT EXISTS eip_read_inputs(owner TEXT,job TEXT,handle TEXT,sha TEXT,data BYTEA,
                      PRIMARY KEY(owner,job,handle));
                    CREATE TABLE IF NOT EXISTS nas_bindings(owner TEXT PRIMARY KEY,account_hash TEXT UNIQUE NOT NULL,
                      ciphertext BYTEA NOT NULL,revision TEXT NOT NULL,updated DOUBLE PRECISION NOT NULL,usable BOOLEAN NOT NULL);
                    CREATE TABLE IF NOT EXISTS nas_login_limits(owner TEXT PRIMARY KEY,next_at DOUBLE PRECISION NOT NULL,request_token TEXT);
                    ALTER TABLE nas_login_limits ADD COLUMN IF NOT EXISTS request_token TEXT;

        CREATE TABLE IF NOT EXISTS web_jobs (
                        id TEXT PRIMARY KEY,owner TEXT NOT NULL,subject TEXT NOT NULL,chat TEXT NOT NULL,
                        request_id TEXT NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL,
                        payload TEXT NOT NULL,engine_job TEXT,progress TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,
                        UNIQUE(owner,chat,request_id));
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS request_aliases TEXT[] NOT NULL DEFAULT '{}';
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'company';
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS document_result TEXT NOT NULL DEFAULT '{}';
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS started DOUBLE PRECISION;
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS chat_revision TEXT NOT NULL DEFAULT '';
        CREATE INDEX IF NOT EXISTS web_jobs_owner_created ON web_jobs(owner,created DESC);
        CREATE INDEX IF NOT EXISTS web_jobs_owner_started ON web_jobs(owner,started DESC) WHERE started IS NOT NULL;
        CREATE INDEX IF NOT EXISTS web_jobs_dispatch ON web_jobs(status,owner,started,created)
    """,
        prepare=False,
    )


def _chat_outbox(db):
    db.execute(
        """
        ALTER TABLE web_jobs ADD COLUMN chat_version BIGINT NOT NULL DEFAULT 1;
        ALTER TABLE web_jobs ADD COLUMN chat_delivered_version BIGINT NOT NULL DEFAULT 0;
        CREATE INDEX web_jobs_chat_pending ON web_jobs(created,id)
          WHERE chat_delivered_version < chat_version AND kind IN ('company','document');
        CREATE INDEX web_jobs_engine ON web_jobs(engine_job,owner);
        CREATE FUNCTION bump_web_job_chat_version() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
          IF ROW(NEW.status,NEW.progress,NEW.engine_job,NEW.request_aliases,NEW.document_result)
             IS DISTINCT FROM ROW(OLD.status,OLD.progress,OLD.engine_job,OLD.request_aliases,OLD.document_result) THEN
            NEW.chat_version := OLD.chat_version + 1;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER web_job_chat_changed BEFORE UPDATE OF status,progress,engine_job,request_aliases,document_result
          ON web_jobs FOR EACH ROW EXECUTE FUNCTION bump_web_job_chat_version();
        CREATE FUNCTION notify_web_job_result() RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
          IF NEW.result IS DISTINCT FROM OLD.result THEN
            UPDATE web_jobs SET chat_version=chat_version+1 WHERE engine_job=NEW.id AND owner=NEW.owner;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER job_result_changed AFTER UPDATE OF result ON jobs
          FOR EACH ROW EXECUTE FUNCTION notify_web_job_result();
        -- Adopt the previous content hash once, without replaying results that
        -- were already delivered. Future polling uses only the indexed version.
        WITH delivered AS (
          SELECT w.id FROM web_jobs w LEFT JOIN jobs j ON j.id=w.engine_job AND j.owner=w.owner
          WHERE w.chat_revision=md5(jsonb_build_array(w.status,w.progress,w.engine_job,
            w.request_aliases,j.result,w.document_result)::text)
        ) UPDATE web_jobs w SET chat_delivered_version=w.chat_version
          FROM delivered d WHERE d.id=w.id;
    """,
        prepare=False,
    )


def _delivery_retries(db):
    db.execute("""
        ALTER TABLE web_jobs ADD COLUMN chat_failures INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE web_jobs ADD COLUMN chat_next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0;
        ALTER TABLE web_jobs ADD COLUMN chat_last_attempt DOUBLE PRECISION;
        CREATE INDEX web_jobs_chat_due ON web_jobs(chat_next_attempt,chat_last_attempt,created,id)
          WHERE chat_delivered_version < chat_version AND kind IN ('company','document');
        CREATE OR REPLACE FUNCTION bump_web_job_chat_version() RETURNS trigger
          LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
          IF ROW(NEW.status,NEW.progress,NEW.engine_job,NEW.request_aliases,NEW.document_result)
             IS DISTINCT FROM ROW(OLD.status,OLD.progress,OLD.engine_job,OLD.request_aliases,OLD.document_result) THEN
            NEW.chat_version := OLD.chat_version + 1;
          END IF;
          IF NEW.chat_version IS DISTINCT FROM OLD.chat_version THEN
            NEW.chat_failures := 0;
            NEW.chat_next_attempt := 0;
            NEW.chat_last_attempt := NULL;
          END IF;
          RETURN NEW;
        END $$;
        DROP TRIGGER web_job_chat_changed ON web_jobs;
        CREATE TRIGGER web_job_chat_changed BEFORE UPDATE OF status,progress,engine_job,
          request_aliases,document_result,chat_version ON web_jobs
          FOR EACH ROW EXECUTE FUNCTION bump_web_job_chat_version();
    """, prepare=False)


def _job_failures(db):
    db.execute("""
        ALTER TABLE web_jobs ADD COLUMN error_code TEXT;
        CREATE OR REPLACE FUNCTION bump_web_job_chat_version() RETURNS trigger
          LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
          IF ROW(NEW.status,NEW.progress,NEW.engine_job,NEW.request_aliases,NEW.document_result,NEW.error_code)
             IS DISTINCT FROM ROW(OLD.status,OLD.progress,OLD.engine_job,OLD.request_aliases,OLD.document_result,OLD.error_code) THEN
            NEW.chat_version := OLD.chat_version + 1;
          END IF;
          IF NEW.chat_version IS DISTINCT FROM OLD.chat_version THEN
            NEW.chat_failures := 0;
            NEW.chat_next_attempt := 0;
            NEW.chat_last_attempt := NULL;
          END IF;
          RETURN NEW;
        END $$;
        DROP TRIGGER web_job_chat_changed ON web_jobs;
        CREATE TRIGGER web_job_chat_changed BEFORE UPDATE OF status,progress,engine_job,
          request_aliases,document_result,chat_version,error_code ON web_jobs
          FOR EACH ROW EXECUTE FUNCTION bump_web_job_chat_version();
    """, prepare=False)
