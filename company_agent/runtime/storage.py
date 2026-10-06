"""Private artifact/job store. Approvals survive restarts; ambiguous writes stop."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from psycopg import sql
from psycopg.conninfo import make_conninfo


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError('請先建立私人 state_dir 資料夾。')
        if self.directory.stat().st_mode & 0o077:
            raise ValueError('state_dir 權限必須為 0700。')
        self.dsn = os.getenv('COMPANY_AGENT_DATABASE_URL')
        self.schema = os.getenv('COMPANY_AGENT_DB_SCHEMA', 'company_agent')
        if not self.dsn:
            raise ValueError('請設定 COMPANY_AGENT_DATABASE_URL；不使用 SQLite 備援。')
        if not re.fullmatch(r'company_agent(?:_[a-z0-9_]+)?', self.schema) or len(self.schema) > 63:
            raise ValueError('Agent PostgreSQL schema 名稱無效。')
        self.conninfo = make_conninfo(
            self.dsn, connect_timeout=5,
            options=f"-c search_path={self.schema} -c lock_timeout=5000 -c statement_timeout=30000",
        )
        with psycopg.connect(self.conninfo) as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_id('schema'),))
            db.execute(
                sql.SQL('CREATE SCHEMA IF NOT EXISTS {}').format(sql.Identifier(self.schema)),
            )
        with self.db() as db:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_id('schema'),))
            db.execute(
                """
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT,status TEXT,result TEXT,created DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,job TEXT,owner TEXT,name TEXT,sha TEXT,data BYTEA);
            CREATE INDEX IF NOT EXISTS artifacts_owner_job ON artifacts(owner,job);
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,job TEXT,owner TEXT,artifact TEXT,root TEXT,path TEXT,status TEXT);
            CREATE TABLE IF NOT EXISTS audit(job TEXT,owner TEXT,action TEXT,status TEXT,created DOUBLE PRECISION);
            CREATE TABLE IF NOT EXISTS model_attempts(job TEXT PRIMARY KEY,attempts INTEGER);
            CREATE TABLE IF NOT EXISTS job_budgets(job TEXT PRIMARY KEY,owner TEXT NOT NULL,step_limit INTEGER NOT NULL CHECK(step_limit BETWEEN 1 AND 80));
            CREATE TABLE IF NOT EXISTS eip_calendar_sources(owner TEXT,job TEXT,artifact TEXT,revision TEXT,
                PRIMARY KEY(owner,job,artifact));
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
            """,
                prepare=False,
            )

    def lock_id(self, key):
        return int.from_bytes(
            hashlib.sha256((self.schema + ':' + key).encode()).digest()[:8], 'big', signed=True,
        )

    def lock(self, db):
        # Serialize short read/modify/write transactions, never model calls.
        db.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_id('mutations'),))

    @contextmanager
    def mutex(self, key):
        with self.db(autocommit=True) as db:
            if not db.execute('SELECT pg_try_advisory_lock(%s)', (self.lock_id(key),)).fetchone()[
                0
            ]:
                raise ValueError('此工作或 worker 正在執行，請勿重複啟動。')
            try:
                yield db
            except BaseException:
                # A lost lease must not replace the original failure/cancellation
                # with another error from trying to unlock a dead connection.
                try:
                    if not db.closed:
                        db.execute('SELECT pg_advisory_unlock(%s)', (self.lock_id(key),))
                except psycopg.Error:
                    pass
                raise
            else:
                db.execute('SELECT pg_advisory_unlock(%s)', (self.lock_id(key),))

    @contextmanager
    def db(self, *, autocommit=False):
        with psycopg.connect(self.conninfo, autocommit=autocommit) as connection:
            yield connection

    def job(self, owner):
        key = str(uuid.uuid4())
        with self.db() as db:
            db.execute(
                'INSERT INTO jobs VALUES(%s,%s,%s,%s,%s)',
                (key, owner, 'running', '{}', time.time()),
            )
        return key

    def finish(self, owner, job, status, result):
        with self.db() as db:
            db.execute(
                'UPDATE jobs SET status=%s,result=%s WHERE id=%s AND owner=%s',
                (status, json.dumps(result, ensure_ascii=False), job, owner),
            )

    def result(self, owner, job):
        with self.db() as db:
            row = db.execute(
                'SELECT status,result FROM jobs WHERE owner=%s AND id=%s', (owner, job),
            ).fetchone()
            if not row:
                raise ValueError('工作不存在或沒有權限。')
            files = self._files(db, owner, job)
        return {
            'job_id': job,
            'status': row[0],
            **json.loads(row[1]),
            'files': files,
        }

    @staticmethod
    def require_job(db, owner, job):
        if not db.execute('SELECT 1 FROM jobs WHERE owner=%s AND id=%s', (owner, job)).fetchone():
            raise ValueError('工作不存在或沒有權限。')

    def artifact(self, owner, job, name, data):
        if len(data) > 20_000_000:
            raise ValueError('產出超過 20 MB 上限。')
        key, sha = str(uuid.uuid4()), hashlib.sha256(data).hexdigest()
        with self.db() as db:
            db.execute(
                'INSERT INTO artifacts VALUES(%s,%s,%s,%s,%s,%s)',
                (key, job, owner, name, sha, data),
            )
        return {'id': key, 'name': name, 'sha256': sha}

    def read_artifact(self, owner, artifact, job=None):
        with self.db() as db:
            row = db.execute(
                'SELECT job,name,sha,data FROM artifacts WHERE owner=%s AND id=%s',
                (owner, artifact),
            ).fetchone()
        if not row or (job is not None and row[0] != job):
            raise ValueError('檔案不存在或不屬於本次工作。')
        if hashlib.sha256(row[3]).hexdigest() != row[2]:
            raise ValueError('產出檔案校驗失敗。')
        return row[1], row[2], row[3]

    def verify_artifacts(self, owner, job, artifacts):
        """Hash only this job's declared artifacts through one bounded cursor.

        A server cursor fetches one blob at a time, including for 100 large
        drafts. Nothing is cached: completion always verifies current bytes.
        """
        expected = set(artifacts)
        if not expected:
            return
        with self.db() as db:
            self.require_job(db, owner, job)
            with db.cursor(name='artifact_validation') as cursor:
                cursor.itersize = 1
                cursor.execute(
                    'SELECT id,sha,data FROM artifacts '
                    'WHERE owner=%s AND job=%s AND id=ANY(%s)',
                    (owner, job, list(expected)),
                )
                verified = set()
                for key, sha, data in cursor:
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise ValueError('產出檔案校驗失敗。')
                    verified.add(key)
        if verified != expected:
            raise ValueError('檔案不存在或不屬於本次工作。')

    def files(self, owner, job):
        with self.db() as db:
            return self._files(db, owner, job)

    @staticmethod
    def _files(db, owner, job):
        rows = db.execute(
            'SELECT id,name,sha FROM artifacts WHERE owner=%s AND job=%s', (owner, job),
        ).fetchall()
        return [{'id': r[0], 'name': r[1], 'sha256': r[2]} for r in rows]

    def audit(self, owner, job, action, status):
        with self.db() as db:
            db.execute(
                'INSERT INTO audit VALUES(%s,%s,%s,%s,%s)',
                (job, owner, action, status, time.time()),
            )

    def propose(self, owner, job, artifact, root, path):
        self.read_artifact(owner, artifact, job)
        with self.db() as db:
            self.lock(db)
            existing = db.execute(
                'SELECT id,status FROM proposals WHERE owner=%s AND job=%s AND artifact=%s AND root=%s AND path=%s',
                (owner, job, artifact, root, path),
            ).fetchone()
            if existing:
                return {
                    'proposal_id': existing[0],
                    'status': existing[1],
                    'root': root,
                    'path': path,
                }
            key = str(uuid.uuid4())
            db.execute(
                'INSERT INTO proposals VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (key, job, owner, artifact, root, path, 'pending'),
            )
        return {
            'proposal_id': key,
            'status': 'pending',
            'root': root,
            'path': path,
            'notice': '尚未寫入，必須使用者在 Agent 之外確認。',
        }

    def proposal(self, owner, job, key):
        with self.db() as db:
            self.require_job(db, owner, job)
            row = db.execute(
                'SELECT artifact,root,path,status FROM proposals WHERE owner=%s AND job=%s AND id=%s',
                (owner, job, key),
            ).fetchone()
        if not row:
            raise ValueError('申請不存在或不屬於本次工作。')
        name, sha, _ = self.read_artifact(owner, row[0], job)
        return {
            'proposal_id': key,
            'artifact_id': row[0],
            'root': row[1],
            'path': row[2],
            'status': row[3],
            'name': name,
            'sha256': sha,
        }

    def reject(self, owner, job, key):
        self.proposal(owner, job, key)
        with self.db() as db:
            self.lock(db)
            status = db.execute(
                'SELECT status FROM proposals WHERE owner=%s AND job=%s AND id=%s',
                (owner, job, key),
            ).fetchone()[0]
            if status not in ('pending', 'rejected'):
                raise ValueError('此申請已提交或結果不明，不可改為拒絕。')
            db.execute(
                "UPDATE proposals SET status='rejected' WHERE owner=%s AND job=%s AND id=%s AND status='pending'",
                (owner, job, key),
            )
        self.audit(owner, job, 'publish', 'rejected')

    def reserve_model_attempt(self, owner, job, limit):
        with self.db() as db:
            self.require_job(db, owner, job)
            row = db.execute(
                'INSERT INTO model_attempts SELECT %s,1 WHERE %s>0 '
                'ON CONFLICT(job) DO UPDATE SET attempts=model_attempts.attempts+1 '
                'WHERE model_attempts.attempts<%s RETURNING attempts', (job, limit, limit),
            ).fetchone()
            if not row:
                count = db.execute('SELECT attempts FROM model_attempts WHERE job=%s', (job,)).fetchone()
                raise ModelCallLimitExceededError(
                    thread_count=count[0] if count else 0, run_count=0, thread_limit=limit, run_limit=None,
                )
        return row[0]

    def step_budget(self, owner, job, initial):
        with self.db() as db:
            self.require_job(db, owner, job)
            db.execute(
                'INSERT INTO job_budgets VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
                (job, owner, initial),
            )
            return db.execute(
                'SELECT step_limit FROM job_budgets WHERE owner=%s AND job=%s', (owner, job),
            ).fetchone()[0]

    def attempts(self, job):
        with self.db() as db:
            row = db.execute('SELECT attempts FROM model_attempts WHERE job=%s', (job,)).fetchone()
        return row[0] if row else 0

    def begin_tool(self, owner, job, call_id, name, arguments, *, limit_names=None, limit=8):
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode(),
        ).hexdigest()
        with self.db() as db:
            self.lock(db)
            row = db.execute(
                'SELECT name,args_hash,result FROM tool_receipts WHERE owner=%s AND job=%s AND call_id=%s',
                (owner, job, call_id),
            ).fetchone()
            if row:
                if row[:2] != (name, digest) or row[2] is None:
                    raise ValueError('先前工具結果不明或參數改變，停止重送；請查看已產生檔案。')
                return json.loads(row[2])
            if limit_names:
                count = db.execute(
                    'SELECT count(*) FROM tool_receipts WHERE owner=%s AND job=%s AND name=ANY(%s)',
                    (owner, job, list(limit_names)),
                ).fetchone()[0]
                if count >= limit:
                    raise ValueError('已達本工作工具呼叫上限；恢復工作不會重設額度。')
            db.execute(
                'INSERT INTO tool_receipts VALUES(%s,%s,%s,%s,%s,NULL)',
                (owner, job, call_id, name, digest),
            )
        return None

    def finish_tool(self, owner, job, call_id, result):
        with self.db() as db:
            db.execute(
                'UPDATE tool_receipts SET result=%s WHERE owner=%s AND job=%s AND call_id=%s',
                (json.dumps(result, ensure_ascii=False), owner, job, call_id),
            )

    def confirm(self, owner, key, access, *, replay_ok=False):
        if access.owner != owner:
            raise ValueError('檔案工具身分與確認者不一致。')
        with self.db() as db:
            self.lock(db)
            row = db.execute(
                'SELECT job,artifact,root,path,status FROM proposals WHERE owner=%s AND id=%s',
                (owner, key),
            ).fetchone()
            if row and row[4] == 'done' and replay_ok:
                access.allowed(row[2], write=True)
                return {'status': 'done', 'root': row[2], 'path': row[3], 'already_completed': True}
            if not row or row[4] != 'pending':
                raise ValueError('申請不存在、已處理或結果不明；不可重送。')
            access.allowed(row[2], write=True)
            _, _, data = self.read_artifact(owner, row[1], row[0])
            db.execute("UPDATE proposals SET status='committing' WHERE id=%s", (key,))
        # Claim committed before external write. A crash leaves committing and
        # can never trigger an automatic second write after service restart.
        access.publish_new(row[2], row[3], data)
        with self.db() as db:
            db.execute("UPDATE proposals SET status='done' WHERE id=%s", (key,))
        self.audit(owner, row[0], 'publish', 'done')
        return {'status': 'done', 'root': row[2], 'path': row[3]}
