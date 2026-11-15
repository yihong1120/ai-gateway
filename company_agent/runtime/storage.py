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
from threading import Lock

import psycopg
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.tools import ToolException
from psycopg.conninfo import make_conninfo

from company_agent.runtime.migrations import migrate
from company_agent.services.files.errors import RecoverableToolError
from examples.shared.runtime.database import connection


class Store:
    def __init__(self, directory, *, dsn=None, schema=None):
        self._lock_metrics_guard = Lock()
        self._lock_metrics = {}
        self.directory = Path(directory)
        if not self.directory.is_dir() or self.directory.is_symlink():
            raise ValueError('請先建立私人 state_dir 資料夾。')
        if self.directory.stat().st_mode & 0o077:
            raise ValueError('state_dir 權限必須為 0700。')
        self.dsn = dsn if dsn is not None else os.getenv('COMPANY_AGENT_DATABASE_URL')
        self.schema = schema if schema is not None else os.getenv('COMPANY_AGENT_DB_SCHEMA', 'company_agent')
        if not self.dsn:
            raise ValueError('請設定 COMPANY_AGENT_DATABASE_URL；不使用 SQLite 備援。')
        if not re.fullmatch(r'company_agent(?:_[a-z0-9_]+)?', self.schema) or len(self.schema) > 63:
            raise ValueError('Agent PostgreSQL schema 名稱無效。')
        self.conninfo = make_conninfo(
            self.dsn, connect_timeout=5,
            options=f"-c search_path={self.schema} -c lock_timeout=5000 -c statement_timeout=30000",
        )
        migrate(self.conninfo, self.schema)

    def lock_id(self, key):
        return int.from_bytes(
            hashlib.sha256((self.schema + ':' + key).encode()).digest()[:8], 'big', signed=True,
        )

    def lock(self, db, *, job=None, owner=None):
        # Queue capacity/claim decisions retain the schema-wide lock. Artifact,
        # delivery and receipt mutations share one lock only within their job.
        if (job is None) != (owner is None):
            raise ValueError('A job lock requires both owner and job.')
        key = 'mutations' if job is None else 'job:' + json.dumps([owner, job], separators=(',', ':'))
        category = 'global' if job is None else 'job'
        started, failed = time.monotonic(), False
        try:
            db.execute('SELECT pg_advisory_xact_lock(%s)', (self.lock_id(key),))
        except BaseException:
            failed = True
            raise
        finally:
            elapsed = time.monotonic() - started
            with self._lock_metrics_guard:
                metrics = self._lock_metrics.setdefault(
                    category, {
                        'acquisitions': 0, 'failures': 0, 'seconds': 0.0, 'max_seconds': 0.0,
                    },
                )
                metrics['acquisitions'] += int(not failed)
                metrics['failures'] += int(failed)
                metrics['seconds'] += elapsed
                metrics['max_seconds'] = max(metrics['max_seconds'], elapsed)

    def lock_metrics(self):
        """Bounded per-process timings without owner/job labels or private data."""
        with self._lock_metrics_guard:
            return {category: dict(values) for category, values in self._lock_metrics.items()}

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
        if autocommit:
            # Session advisory leases must never return to the transaction pool.
            with psycopg.connect(self.conninfo, autocommit=True) as db:
                yield db
        else:
            with connection(self.dsn, self.schema) as db:
                yield db

    def job(self, owner):
        key = str(uuid.uuid4())
        with self.db() as db:
            db.execute(
                'INSERT INTO jobs(id,owner,status,result,created) VALUES(%s,%s,%s,%s,%s)',
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
            history = self._files(db, owner, job)
        return {
            'job_id': job,
            'status': row[0],
            **json.loads(row[1]),
            'files': [f for f in history if f['lifecycle'] != 'superseded'],
            'artifact_history': [f for f in history if f['lifecycle'] == 'superseded'],
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
            self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            if db.execute('SELECT delivery_finalized FROM jobs WHERE owner=%s AND id=%s', (owner, job)).fetchone()[0]:
                raise ValueError('本工作已完成交付驗收，不可再新增成果。')
            db.execute(
                'INSERT INTO artifacts(id,job,owner,name,sha,data) VALUES(%s,%s,%s,%s,%s,%s)',
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

    def revise_artifact(self, owner, job, old_id, old_sha, data, call_id):
        """Commit a new immutable version, supersession and receipt together."""

        if len(data) > 20_000_000:
            raise ValueError('產出超過 20 MB 上限。')
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            receipt = db.execute(
                'SELECT name,result FROM tool_receipts WHERE owner=%s AND job=%s AND call_id=%s',
                (owner, job, call_id),
            ).fetchone()
            if not receipt or receipt[0] != 'edit_artifact_text_run':
                raise ValueError('缺少本次修訂工具的執行紀錄。')
            if receipt[1] is not None:
                return json.loads(receipt[1])
            old = db.execute(
                'SELECT name,sha,data,lifecycle FROM artifacts WHERE owner=%s AND job=%s AND id=%s FOR UPDATE',
                (owner, job, old_id),
            ).fetchone()
            if not old or old[1] != old_sha or hashlib.sha256(old[2]).hexdigest() != old_sha:
                raise ValueError('修訂來源不屬於本工作或版本校驗失敗。')
            if db.execute('SELECT delivery_finalized FROM jobs WHERE owner=%s AND id=%s', (owner, job)).fetchone()[0]:
                raise ValueError('本工作已完成交付驗收，不可再修訂成果。')
            if old[3] != 'draft':
                raise RecoverableToolError('revision_source_inactive', '只能修訂有效草稿，請重新讀取真正最新版；不從舊版本建立分支。')
            active = db.execute(
                "SELECT id FROM artifacts WHERE owner=%s AND job=%s AND lower(name)=lower(%s) AND lifecycle!='superseded'",
                (owner, job, old[0]),
            ).fetchall()
            if {row[0] for row in active} != {old_id}:
                raise RecoverableToolError('ambiguous_revision_source', '同名有效草稿不唯一，請由主協調明確處理版本關係後再修訂。')
            key, sha = str(uuid.uuid4()), hashlib.sha256(data).hexdigest()
            result = {
                'id': key, 'name': old[0], 'sha256': sha,
                'revises': {'artifact_id': old_id, 'sha256': old_sha}, 'supersedes': old_id,
                'notice': '已建立同一交付名稱的新版本；舊版保留稽核但不交付。新版須重新驗收與獨立審核，未寫入 NAS。',
            }
            db.execute(
                'INSERT INTO artifacts(id,job,owner,name,sha,data) VALUES(%s,%s,%s,%s,%s,%s)',
                (key, job, owner, old[0], sha, data),
            )
            db.execute(
                "UPDATE artifacts SET lifecycle='superseded',superseded_by=%s WHERE owner=%s AND job=%s AND id=%s",
                (key, owner, job, old_id),
            )
            db.execute(
                'UPDATE tool_receipts SET result=%s WHERE owner=%s AND job=%s AND call_id=%s',
                (json.dumps(result, ensure_ascii=False), owner, job, call_id),
            )
            db.execute(
                'INSERT INTO audit VALUES(%s,%s,%s,%s,%s)',
                (job, owner, 'revise_artifact', old_id+'->'+key, time.time()),
            )
        return result

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

    def deliverable_files(self, owner, job):
        return [f for f in self.files(owner, job) if f['lifecycle'] != 'superseded']

    def supersede_artifact(self, owner, job, old_id, old_sha, new_id, new_sha):
        """Explicit, atomic and idempotent version transition; never delete bytes."""
        if old_id == new_id:
            raise ValueError('新版與舊版不可為相同 ID。')
        # Ownership and byte integrity checks also apply to historical versions.
        for key, expected in ((old_id, old_sha), (new_id, new_sha)):
            if self.read_artifact(owner, key, job)[1] != expected:
                raise ValueError('成果版本不符，未取代。')
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
            rows = db.execute(
                'SELECT id,lifecycle,superseded_by,name FROM artifacts WHERE owner=%s AND job=%s AND id=ANY(%s) FOR UPDATE',
                (owner, job, [old_id, new_id]),
            ).fetchall()
            items = {r[0]: r for r in rows}
            if len(items) != 2:
                raise ValueError('成果不屬於本工作。')
            old, new = items[old_id], items[new_id]
            if old[1] == 'superseded' and old[2] == new_id:
                return {'superseded': old_id, 'replacement': new_id}
            if old[1] != 'draft' or new[1] != 'draft':
                raise ValueError('只能取代尚未交付的有效草稿，不可分支取代或形成循環。')
            plan = db.execute('SELECT payload FROM delivery_plans WHERE owner=%s AND job=%s', (owner, job)).fetchone()
            if plan and old[3] != new[3] and old[3] in {f['name'] for f in json.loads(plan[0])['files']}:
                raise ToolException('交付規格要求保留舊檔名，不可用不同檔名取代；請保留兩份交付並分別驗收、審核。')
            if Path(old[3]).suffix.lower() != Path(new[3]).suffix.lower():
                raise ValueError('新舊成果格式必須一致。')
            db.execute(
                "UPDATE artifacts SET lifecycle='superseded',superseded_by=%s WHERE owner=%s AND job=%s AND id=%s",
                (new_id, owner, job, old_id),
            )
            db.execute(
                'INSERT INTO audit VALUES(%s,%s,%s,%s,%s)',
                (job, owner, 'supersede_artifact', old_id + '->' + new_id, time.time()),
            )
        return {'superseded': old_id, 'replacement': new_id}

    def finalize_artifacts(self, owner, job, expected):
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            files = [f for f in self._files(db, owner, job) if f['lifecycle'] != 'superseded']
            if {f['id'] for f in files} != set(expected):
                raise ValueError('產出清單已變更，請重新驗收。')
            db.execute(
                "UPDATE artifacts SET lifecycle='final' WHERE owner=%s AND job=%s AND lifecycle='draft'",
                (owner, job),
            )
            db.execute('UPDATE jobs SET delivery_finalized=TRUE WHERE owner=%s AND id=%s', (owner, job))

    def save_evidence(self, owner, job, key, payload):
        value = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            row = db.execute(
                'SELECT payload FROM analysis_evidence WHERE owner=%s AND job=%s AND id=%s',
                (owner, job, key),
            ).fetchone()
            if row and row[0] != value:
                raise ValueError('統計憑據不可改寫。')
            db.execute(
                'INSERT INTO analysis_evidence VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (owner, job, key, value),
            )
        return {'evidence_id': key, **payload}

    def delivery_plan(self, owner, job, payload=None):
        with self.db() as db:
            if payload is not None:
                self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            row = db.execute('SELECT payload FROM delivery_plans WHERE owner=%s AND job=%s', (owner, job)).fetchone()
            current = json.loads(row[0]) if row else None
            if payload is None:
                return current
            if current == payload:
                return current
            if current is not None or self._files(db, owner, job):
                raise ValueError('交付規格須在產檔前固定；不可修改已登記規格。請另開工作處理規格變更。')
            db.execute(
                'INSERT INTO delivery_plans VALUES(%s,%s,%s)',
                (owner, job, json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )
        return payload

    def evidence(self, owner, job, keys=None):
        with self.db() as db:
            rows = db.execute(
                'SELECT id,payload FROM analysis_evidence WHERE owner=%s AND job=%s ORDER BY id',
                (owner, job),
            ).fetchall()
        values = {r[0]: {'evidence_id': r[0], **json.loads(r[1])} for r in rows}
        if keys is not None:
            if not set(keys) <= values.keys():
                raise ValueError('缺少本工作實際執行的統計憑據；請先完成資料工具。')
            return [values[key] for key in keys]
        return list(values.values())

    def save_validation(self, owner, job, artifact, sha, payload):
        if self.read_artifact(owner, artifact, job)[1] != sha:
            raise ValueError('驗收版本不符。')
        with self.db() as db:
            db.execute(
                'INSERT INTO artifact_validations VALUES(%s,%s,%s,%s,%s) '
                'ON CONFLICT(owner,job,artifact,sha) DO UPDATE SET payload=EXCLUDED.payload',
                (owner, job, artifact, sha, json.dumps(payload, ensure_ascii=False)),
            )

    def validations(self, owner, job):
        with self.db() as db:
            return [{'artifact_id': r[0], 'sha256': r[1], **json.loads(r[2])} for r in db.execute(
                'SELECT artifact,sha,payload FROM artifact_validations WHERE owner=%s AND job=%s',
                (owner, job),
            ).fetchall()]

    @staticmethod
    def _files(db, owner, job):
        rows = db.execute(
            'SELECT id,name,sha,lifecycle,superseded_by FROM artifacts WHERE owner=%s AND job=%s ORDER BY id', (
                owner, job,
            ),
        ).fetchall()
        return [{'id': r[0], 'name': r[1], 'sha256': r[2], 'lifecycle': r[3], 'superseded_by': r[4]} for r in rows]

    def audit(self, owner, job, action, status):
        with self.db() as db:
            db.execute(
                'INSERT INTO audit VALUES(%s,%s,%s,%s,%s)',
                (job, owner, action, status, time.time()),
            )

    def propose(self, owner, job, artifact, root, path):
        self.read_artifact(owner, artifact, job)
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
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
            self.lock(db, owner=owner, job=job)
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

    def claim_assignment(self, owner, job, call_id, work_key, payload, replaces=()):
        """Reserve work and output ownership atomically, before any child runs.

        Identical native-call replay is permitted for checkpoint/approval resume;
        a different call cannot repeat a key or claim the same active outputs.
        Caller verifies explicit revision/recovery references against reports.
        """

        with self.db() as db:
            self.lock(db, owner=owner, job=job)
            self.require_job(db, owner, job)
            rows = db.execute(
                'SELECT work_key,call_id,payload FROM assignment_claims WHERE owner=%s AND job=%s ORDER BY sequence',
                (owner, job),
            ).fetchall()
            claims = [(key, call, json.loads(value)) for key, call, value in rows]
            for key, call, value in claims:
                if call == call_id:
                    if key != work_key or value != payload:
                        raise ValueError('委派恢復參數已改變，停止重送。')
                    return
                if key == work_key or value == payload:
                    raise RecoverableToolError(
                        'duplicate_assignment', '此工作已派發；請使用既有結果或等待原工作，不重複呼叫子角色。修訂／補做須使用新 work_key 並明列前次委派 ID。',
                        assignment_id=call, work_key=key,
                    )
            latest = {}
            for _, call, value in claims:
                for name in value['outputs']:
                    latest[name.casefold()] = (call, value['role'])
            for name in payload['outputs']:
                previous = latest.get(name.casefold())
                if previous and (previous[0] not in replaces or previous[1] != payload['role']):
                    raise RecoverableToolError(
                        'output_already_assigned', '此檔案已有負責子任務；不可另派工作重做。修訂或補做須明確引用前次委派。',
                        name=name, assignment_id=previous[0],
                    )
            db.execute(
                'INSERT INTO assignment_claims(owner,job,work_key,call_id,payload) VALUES(%s,%s,%s,%s,%s)',
                (owner, job, work_key, call_id, json.dumps(payload, ensure_ascii=False)),
            )

    def begin_tool(self, owner, job, call_id, name, arguments, *, limit_names=None, limit=8):
        digest = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode(),
        ).hexdigest()
        with self.db() as db:
            self.lock(db, owner=owner, job=job)
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
            proposal = db.execute('SELECT job FROM proposals WHERE owner=%s AND id=%s', (owner, key)).fetchone()
            if not proposal:
                raise ValueError('申請不存在或沒有權限。')
            self.lock(db, owner=owner, job=proposal[0])
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
