"""Encrypted opt-in credentials and durable hourly task checkpoints.

The encryption key is a separate 0600 file outside the database directory.
Changing/loss of the key NEVER silently resets credentials or write history.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.rows import dict_row

import examples.official_document_workflow.domain.calendar.tracking_reminders as _tracking_reminders_imports
from .database import configuration
from .database import connection
from .migrations import migrate
from examples.shared.runtime.modules import ModuleReference

_workflow_runtime = ModuleReference('examples.official_document_workflow.runtime')


class AutomationStore:
    def __init__(self, key_path: Path, *, dsn=None, schema=None):
        self.dsn, self.schema = (dsn, schema) if dsn is not None and schema is not None else configuration()
        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # All workers initialize/read this shared file under one database lock,
        # so none can observe a partially written key on their first request.
        with psycopg.connect(
            self.dsn, connect_timeout=5, options='-c lock_timeout=5000',
        ) as db:
            db.execute(
                'SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                ('official-document-key:' + str(key_path.absolute()),),
            )
            if not key_path.exists():
                for table in (
                    'accounts',
                    'tasks',
                    'processing_chunks',
                    'shared_content_cache',
                    'document_analysis_results',
                    'calendar_managed_events',
                    'pending_clarifications',
                    'clarification_outputs',
                    'clarification_output_reviews',
                ):
                    exists = db.execute(
                        'SELECT to_regclass(%s)', (f'{self.schema}.{table}',),
                    ).fetchone()[0]
                    if (
                        exists
                        and db.execute(
                            sql.SQL('SELECT 1 FROM {}.{} LIMIT 1').format(
                                sql.Identifier(self.schema),
                                sql.Identifier(table),
                            ),
                        ).fetchone()
                    ):
                        raise ValueError(
                            '自動處理加密金鑰遺失，已停止讀取既有憑證與處理紀錄。',
                        )
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key())
                    stream.flush()
                    os.fsync(stream.fileno())
            info = key_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('自動處理加密金鑰必須為獨立的 0600 一般檔案。')
            self.cipher = Fernet(key_path.read_bytes())
        migrate(self.dsn, self.schema)

    def db(self):
        return connection(self.dsn, self.schema, row_factory=dict_row)

    def encode(self, owner, value):
        return self.cipher.encrypt(
            json.dumps({'owner': owner, 'value': value}, ensure_ascii=False).encode(),
        )

    def decode(self, owner, value):
        data = json.loads(self.cipher.decrypt(value))
        if data['owner'] != owner:
            raise ValueError()
        return data['value']

    def bind(self, owner, identity, account, password, *, enable_auto=True):
        """A verified login is required by the caller before persisting a secret."""
        generation = secrets.token_urlsafe(24)
        with self.db() as db:
            other = db.execute(
                'SELECT owner FROM accounts WHERE identity=%s', (identity,),
            ).fetchone()
            if other and other['owner'] != owner:
                raise ValueError(
                    '此 EIP 帳號已綁定另一個 WebUI 使用者的自動處理，請先解除原綁定。',
                )
            if (
                db.execute('SELECT count(*) AS total FROM accounts').fetchone()['total']
                >= 64
                and not db.execute(
                    'SELECT 1 FROM accounts WHERE owner=%s', (owner,),
                ).fetchone()
            ):
                raise ValueError('自動處理帳號數已達上限。')
            db.execute(
                'INSERT INTO scan_origins VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                (owner, identity, time.time() - 86400),
            )
            cutoff = db.execute(
                'SELECT scan_start FROM scan_origins WHERE owner=%s AND identity=%s',
                (owner, identity),
            ).fetchone()['scan_start']
            db.execute(
                """INSERT INTO accounts(owner,identity,secret,generation,enabled,next_run,scan_start,message)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(owner) DO UPDATE SET identity=excluded.identity,
                secret=excluded.secret,generation=excluded.generation,enabled=excluded.enabled,next_run=excluded.next_run,
                scan_start=excluded.scan_start,
                failures=0,message=excluded.message""",
                (
                    owner,
                    identity,
                    self.encode(
                        owner,
                        {
                            'account': account,
                            'password': password,
                        },
                    ),
                    generation,
                    enable_auto,
                    time.time() + 15,
                    cutoff,
                    '已啟用，等待首次自動處理' if enable_auto else '已保存憑證；自動處理未啟用',
                ),
            )

    def account(self, owner, *, secrets=False):
        with self.db() as db:
            row = db.execute(
                'SELECT * FROM accounts WHERE owner=%s', (owner,),
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        encrypted = data.pop('secret')

        data['tracking_reminders_enabled'] = _tracking_reminders_imports.enabled_for(owner, data['identity'])
        if secrets and encrypted:
            data.update(self.decode(owner, encrypted))
        return data

    def active(self, owner, generation):
        with self.db() as db:
            return (
                db.execute(
                    'SELECT 1 FROM accounts WHERE owner=%s AND generation=%s AND enabled',
                    (owner, generation),
                ).fetchone()
                is not None
            )

    def set_calendar_attach_renamed_pdf(self, owner, enabled: bool):
        with self.db() as db:
            if (
                db.execute(
                    'UPDATE accounts SET calendar_attach_renamed_pdf=%s WHERE owner=%s',
                    (enabled, owner),
                ).rowcount
                != 1
            ):
                raise ValueError('找不到已綁定的 EIP 帳號。')

    def pause(self, owner, *, forget=False):
        with self.db() as db:
            if forget:
                # Keep task checkpoints and calendar journal, never credentials.
                db.execute('DELETE FROM accounts WHERE owner=%s', (owner,))
            else:
                db.execute(
                    "UPDATE accounts SET enabled=FALSE,generation=%s,message='已暫停' WHERE owner=%s",
                    (secrets.token_urlsafe(24), owner),
                )

    def claim_due(self, limit=64, excluded_owners=()):
        now = time.time()
        with self.db() as db:
            rows = db.execute(
                'SELECT owner,generation FROM accounts WHERE enabled=TRUE AND next_run<=%s AND NOT(owner=ANY(%s)) ORDER BY next_run,owner LIMIT %s FOR UPDATE SKIP LOCKED',
                (
                    now,
                    list(
                        excluded_owners,
                    ),
                    limit,
                ),
            ).fetchall()
            owners = [row['owner'] for row in rows]
            if owners:
                db.execute(
                    "UPDATE accounts SET next_run=%s,last_run=%s,message='執行中' WHERE owner=ANY(%s)",
                    (now + 3600, now, owners),
                )
        return [dict(row) for row in rows]

    def finish(self, owner, generation, message, failed=False):
        with self.db() as db:
            db.execute(
                """UPDATE accounts SET message=%s, failures=CASE WHEN %s THEN failures+1 ELSE 0 END
                WHERE owner=%s AND generation=%s AND enabled=TRUE""",
                (message, failed, owner, generation),
            )
            # Failures are health information, not withdrawal of user consent.
            # claim_due limits retries to once per hour; manual pause still wins.

    def discover(self, owner, identity, docs):
        # Commit discovery BEFORE asking Detail (which may change EIP read state).
        now = time.time()
        with self.db() as db:
            for doc in docs:
                key = hashlib.sha256(
                    json.dumps([owner, identity, doc['id']]).encode(),
                ).hexdigest()
                inserted = (
                    db.execute(
                        'INSERT INTO tasks(id,owner,identity,document_id,phase,payload,updated) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                        (
                            key,
                            owner,
                            identity,
                            doc['id'],
                            'discovered',
                            self.encode(owner, {'document': doc}),
                            now,
                        ),
                    ).rowcount
                    == 1
                )
                # Independent of the replaceable task row: explicit reprocessing
                # cannot erase the first observation. Legacy unknowns stay NULL.
                db.execute(
                    'INSERT INTO task_timeline(task_id,first_detected_at) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                    (key, now if inserted else None),
                )
                row = db.execute('SELECT * FROM tasks WHERE id=%s', (key,)).fetchone()
                if row['phase'] == 'needs_review':
                    continue
                if row['phase'] == 'outside_scope' and doc.get('folder') == 'inbox':
                    payload = self.decode(owner, row['payload'])
                    # Reopen only date-skipped work which never downloaded/produced/wrote.
                    if not any(
                        payload.get(k) for k in ('source_ids', 'output', 'import')
                    ):
                        payload['document'] = doc
                        payload['notice'] = (
                            '目前仍在待處理文件匣，已依不限日期規則納入。'
                        )
                        db.execute(
                            'UPDATE tasks SET payload=%s,phase=%s,updated=%s WHERE id=%s',
                            (self.encode(owner, payload), 'discovered', now, key),
                        )
                    continue
                if row['phase'] not in ('discovered', 'no_pdf'):
                    # Completed, interrupted and ambiguous writes are never reset.
                    continue
                payload = self.decode(owner, row['payload'])
                payload['document'] = doc  # Use current folder after a flow moves.
                # An attachment list can be populated a little after the inbox
                # item appears.  Do one delayed confirmation, but never use an
                # empty attachment list as an unbounded hourly work queue.
                # ``no_pdf_recheck_started`` is persisted before the second
                # request, so a crash cannot reset the limit and make the task
                # scan forever.
                recheck = (
                    row['phase'] == 'no_pdf'
                    and not payload.get('no_pdf_recheck_started')
                    and row['updated'] <= now - 3600
                )
                if recheck:
                    payload['no_pdf_recheck_started'] = True
                    payload['notice'] = '等待重新檢查是否補上 PDF／Word 附件。'
                db.execute(
                    'UPDATE tasks SET payload=%s,phase=%s,updated=%s WHERE id=%s',
                    (
                        self.encode(owner, payload),
                        'discovered' if recheck else row['phase'],
                        now if recheck else row['updated'],
                        key,
                    ),
                )

    def tasks(self, owner, identity=None, *, pending=False):
        sql = 'SELECT * FROM tasks WHERE owner=%s'
        params = [owner]
        if identity is not None:
            sql += ' AND identity=%s'
            params.append(identity)
        if pending:
            sql += " AND phase IN ('discovered','downloaded','converted') AND retry_at=0 ORDER BY updated LIMIT 10"
        else:
            sql += ' ORDER BY updated DESC LIMIT 100'
        with self.db() as db:
            rows = db.execute(sql, params).fetchall()
        return [
            {**dict(row), 'payload': self.decode(owner, row['payload'])} for row in rows
        ]

    def task_page(self, owner, page=1, page_size=5):
        """Owner-scoped history; decrypt only the requested display page."""
        if page < 1 or not 1 <= page_size <= 20:
            raise ValueError('Invalid history pagination')
        with self.db() as db:
            db.execute('BEGIN')
            total = db.execute(
                'SELECT count(*) AS total FROM tasks WHERE owner=%s', (owner,),
            ).fetchone()['total']
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = db.execute(
                """SELECT tasks.*,task_timeline.first_detected_at,task_timeline.last_processed_at
                FROM tasks LEFT JOIN task_timeline ON task_timeline.task_id=tasks.id
                WHERE owner=%s ORDER BY updated DESC,id DESC LIMIT %s OFFSET %s""",
                (owner, page_size, (page - 1) * page_size),
            ).fetchall()
        return (
            [
                {**dict(row), 'payload': self.decode(owner, row['payload'])}
                for row in rows
            ],
            {'page': page, 'page_size': page_size, 'total': total, 'pages': pages},
        )

    def save_task(self, task, phase, payload):
        now = time.time()
        with self.db() as db:
            db.execute(
                'UPDATE tasks SET phase=%s,payload=%s,updated=%s,retry_at=0 WHERE id=%s AND owner=%s',
                (
                    phase,
                    self.encode(task['owner'], payload),
                    now,
                    task['id'],
                    task['owner'],
                ),
            )
            db.execute(
                """INSERT INTO task_timeline(task_id,last_processed_at)
                SELECT id,%s FROM tasks WHERE id=%s AND owner=%s
                ON CONFLICT(task_id) DO UPDATE SET last_processed_at=excluded.last_processed_at""",
                (now, task['id'], task['owner']),
            )
        task.update(phase=phase, payload=payload, retry_at=0)

    def recover(self):
        # Conversion may have created files/spent tokens before a process crash.
        # Do not blindly repeat it or repeat ambiguous external writes.
        with self.db() as db:
            rows = db.execute(
                "UPDATE tasks SET phase='needs_review',retry_at=0 WHERE phase IN ('downloading','processing','importing','assigning_projects','attaching') OR (retry_at>0 AND phase IN ('discovered','downloaded','converted')) RETURNING id,owner,payload",
            ).fetchall()
            for row in rows:
                payload = self.decode(row['owner'], row['payload'])
                payload['notice'] = (
                    '先前執行中斷或屬於舊的失敗續跑佇列，已停止自動處理；請人工核對。'
                )
                db.execute(
                    'UPDATE tasks SET payload=%s WHERE id=%s',
                    (self.encode(row['owner'], payload), row['id']),
                )


def key_file():
    return Path(
        os.getenv(
            'EIP_AUTOMATION_KEY_FILE',
            str(Path.home() / '.local/state/ai-gateway/secrets/eip-automation.key'),
        ),
    )


def store():
    current_resources = _workflow_runtime.attribute('current_resources')
    return current_resources().store
