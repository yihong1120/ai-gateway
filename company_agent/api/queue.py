"""Durable owned jobs, source snapshots and one-query dashboard projections."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from psycopg.rows import dict_row

from company_agent.config import MAX_STEPS
from company_agent.runtime.storage import Store

SUPPORTED = {
    '.pdf',
    '.docx',
    '.pptx',
    '.xlsx',
    '.txt',
    '.md',
    '.csv',
    '.dwg',
    '.dxf',
    '.png',
    '.jpg',
    '.jpeg',
}
# Include historical statuses so older completed records remain readable.
TERMINAL = {
    'done',
    'partial',
    'error',
    'needs_input',
    'blocked',
    'interrupted',
    'cancelled',
    'awaiting_approval',
}


class WorkQueue:
    def __init__(self, state_dir, source_dir, eip_binding=None, *, max_pending=500, max_per_user=20):
        if max_pending < 1 or max_per_user < 1:
            raise ValueError('工作佇列上限必須大於零。')
        self.max_pending, self.max_per_user = max_pending, max_per_user
        self.eip_binding = eip_binding
        self.store = Store(state_dir)
        self.sources = Path(source_dir)
        self.sources.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.sources.is_symlink() or self.sources.stat().st_mode & 0o077:
            raise ValueError('附件目錄必須為私人一般資料夾。')
        with self.store.db() as db:
            self.store.lock(db)
            db.execute("""CREATE TABLE IF NOT EXISTS web_jobs (
                id TEXT PRIMARY KEY,owner TEXT NOT NULL,subject TEXT NOT NULL,chat TEXT NOT NULL,
                request_id TEXT NOT NULL,request_hash TEXT NOT NULL,status TEXT NOT NULL,
                payload TEXT NOT NULL,engine_job TEXT,progress TEXT NOT NULL,created DOUBLE PRECISION NOT NULL,
                UNIQUE(owner,chat,request_id))""")
            db.execute("ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS request_aliases TEXT[] NOT NULL DEFAULT '{}'")
            db.execute("ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'company'")
            db.execute("ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS document_result TEXT NOT NULL DEFAULT '{}'")
            db.execute('ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS started DOUBLE PRECISION')
            db.execute('ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE')
            db.execute("ALTER TABLE web_jobs ADD COLUMN IF NOT EXISTS chat_revision TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE INDEX IF NOT EXISTS web_jobs_owner_created ON web_jobs(owner,created DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS web_jobs_owner_started ON web_jobs(owner,started DESC) WHERE started IS NOT NULL')
            db.execute('CREATE INDEX IF NOT EXISTS web_jobs_dispatch ON web_jobs(status,owner,started,created)')

    def check_capacity(self, db, owner):
        total, owned = db.execute(
            'SELECT count(*),count(*) FILTER (WHERE owner=%s) FROM web_jobs '
            "WHERE status IN ('queued','running')", (owner,),
        ).fetchone()
        if total >= self.max_pending:
            raise ValueError('工作佇列已滿，請稍後再送。')
        if owned >= self.max_per_user:
            raise ValueError('本人的待處理工作已達上限，請等待完成或取消部分工作。')

    def submit(self, owner, subject, chat, request_id, instruction, inputs, references=(), conversation=(), *, allow_pending_references=False):
        from company_agent.services.files.nas import NASVault

        binding = NASVault(self.store).status(owner)
        nas_revision = binding['revision'] if binding['usable'] else None
        eip_revision = self.eip_binding(owner) if self.eip_binding else None
        if not all(
            isinstance(v, str) and v.strip() and len(v) <= 255
            for v in (owner, subject, chat, request_id)
        ):
            raise ValueError('缺少有效工作識別。')
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 10000:
            raise ValueError('請輸入 10000 字以內的工作指令。')
        if len(inputs) > 20 or sum(len(data) for _, data in inputs) > 50_000_000:
            raise ValueError('本批附件超過資源上限（20 份／50 MB），請分批。')
        digest = hashlib.sha256(
            json.dumps(
                [
                    instruction,
                    references,
                    conversation,
                    [(name, hashlib.sha256(data).hexdigest()) for name, data in inputs],
                ],
                ensure_ascii=False,
            ).encode(),
        ).hexdigest()
        with self.store.db() as db:
            self.store.lock(db)
            prior = db.execute(
                'SELECT id,request_hash FROM web_jobs WHERE owner=%s AND chat=%s AND (request_id=%s OR %s=ANY(request_aliases))',
                (owner, chat, request_id, request_id),
            ).fetchone()
            if prior:
                if prior[1] != digest:
                    raise ValueError('同一訊息內容已變更；請另送新訊息，不重播舊操作。')
                return prior[0]
            # Regeneration creates a new assistant ID while the same owned work
            # is still running. Attach that ID to the existing work and its sources.
            active = db.execute(
                'SELECT id,payload FROM web_jobs WHERE owner=%s AND subject=%s AND chat=%s '
                "AND request_hash=%s AND status IN ('queued','running') ORDER BY created,id",
                (owner, subject, chat, digest),
            ).fetchall()
            for key, stored in active:
                payload = json.loads(stored)
                if (payload.get('nas_binding_revision'), payload.get('eip_binding_revision')) == (nas_revision, eip_revision):
                    db.execute(
                        'UPDATE web_jobs SET request_aliases=array_append(request_aliases,%s) WHERE id=%s', (
                            request_id, key,
                        ),
                    )
                    return key
            key = str(uuid.uuid4())
            self.check_capacity(db, owner)
            manifest = []
            for name, data in inputs:
                if (
                    not isinstance(name, str)
                    or Path(name).suffix.lower() not in SUPPORTED
                    or not data
                    or len(data) > 20_000_000
                ):
                    raise ValueError(
                        '附件格式或大小不支援；接受 PDF、DOCX、PPTX、XLSX、TXT、MD、CSV、DWG、DXF，單檔 20 MB。',
                    )
            for reference in references:
                old = self.get(owner, reference)
                for field, revision in (('nas_binding_revision', nas_revision), ('eip_binding_revision', eip_revision)):
                    if old['payload'].get(field) and old['payload'][field] != revision:
                        raise ValueError(f"歷史工作的 {field.split('_')[0].upper()} 帳號已解除或變更，請另開聊天室。")
                if (
                    old['chat'] != chat
                    or old['subject'] != subject
                    or old['kind'] != 'company'
                    or old['status'] not in (TERMINAL - {'awaiting_approval'} | ({'queued', 'running'} if allow_pending_references else set()))
                ):
                    raise ValueError('僅能引用同一聊天室、同一計費身分且已停止的本人工作。')
            directory = self.sources / key
            directory.mkdir(mode=0o700)
            for index, (name, data) in enumerate(inputs):
                filename = f"input_{index+1}" + Path(name).suffix.lower()
                with open(directory / filename, 'xb') as stream:
                    stream.write(data)
                os.chmod(directory / filename, 0o600)
                manifest.append({'path': filename, 'display_name': name})
            payload = {
                'instruction': instruction,
                'manifest': manifest,
                'references': references,
                'conversation': conversation,
                'operation': 'run',
                'nas_binding_revision': nas_revision,
                'eip_binding_revision': eip_revision,
            }
            db.execute(
                'INSERT INTO web_jobs (id,owner,subject,chat,request_id,request_hash,status,payload,engine_job,progress,created) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (
                    key,
                    owner,
                    subject,
                    chat,
                    request_id,
                    digest,
                    'queued',
                    json.dumps(payload, ensure_ascii=False),
                    None,
                    '等待執行',
                    time.time(),
                ),
            )
        return key

    def submit_document(self, owner, chat, request_id, sources, valves, content_hash):
        """Only the authenticated upload bridge constructs this fixed workflow."""
        if not all(
            isinstance(v, str) and v.strip() and len(v) <= 255
            for v in (owner, chat, request_id)
        ):
            raise ValueError('缺少有效工作識別。')
        if not sources or len(sources) > 50:
            raise ValueError('請附加本次公文，並依批次上限分批。')
        payload = {
            'operation': 'document', 'sources': sources,
            'valves': valves, 'content_hash': content_hash,
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self.store.db() as db:
            self.store.lock(db)
            prior = db.execute(
                'SELECT id,request_hash FROM web_jobs WHERE owner=%s AND chat=%s '
                'AND (request_id=%s OR %s=ANY(request_aliases))', (owner, chat, request_id, request_id),
            ).fetchone()
            if prior:
                if prior[1] != digest:
                    raise ValueError('同一訊息附件或設定已變更，請另送新訊息。')
                return prior[0]
            active = db.execute(
                "SELECT id FROM web_jobs WHERE owner=%s AND chat=%s AND kind='document' "
                "AND request_hash=%s AND status IN ('queued','running') ORDER BY created,id",
                (owner, chat, digest),
            ).fetchone()
            if active:
                db.execute(
                    'UPDATE web_jobs SET request_aliases=array_append(request_aliases,%s) WHERE id=%s',
                    (request_id, active[0]),
                )
                return active[0]
            self.check_capacity(db, owner)
            key = str(uuid.uuid4())
            db.execute(
                'INSERT INTO web_jobs(id,owner,subject,chat,request_id,request_hash,status,payload,progress,created,kind) '
                "VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,'等待公文背景處理',%s,'document')",
                (
                    key, owner, 'document:' + owner, chat, request_id, digest,
                    json.dumps(payload, ensure_ascii=False), time.time(),
                ),
            )
        return key

    def get(self, owner, key):
        with self.store.db() as db:
            db.row_factory = dict_row
            row = db.execute(
                'SELECT * FROM web_jobs WHERE owner=%s AND id=%s', (owner, key),
            ).fetchone()
        if not row:
            raise ValueError('工作不存在或沒有權限。')
        value = dict(row)
        value['payload'] = json.loads(value['payload'])
        return value

    def public(self, owner, key):
        rows = self._public(owner, key)
        if not rows:
            raise ValueError('工作不存在或沒有權限。')
        return rows[0]

    def list(self, owner):
        return self._public(owner)

    def chat_pending(self, limit=32):
        """Bounded durable outbox; observe job changes without fetching input payloads."""
        with self.store.db() as db:
            db.row_factory = dict_row
            return db.execute(
                """WITH updates AS (
                SELECT w.id,w.owner,w.chat,w.request_id,w.request_aliases,w.chat_revision,
                    md5(jsonb_build_array(w.status,w.progress,w.engine_job,
                        w.request_aliases,j.result)::text) AS revision,w.created
                FROM web_jobs w LEFT JOIN jobs j ON j.id=w.engine_job AND j.owner=w.owner
                WHERE w.kind='company'
            ) SELECT * FROM updates WHERE revision<>chat_revision
              ORDER BY created,id LIMIT %s""", (limit,),
            ).fetchall()

    def chat_delivered(self, key, revision):
        with self.store.db() as db:
            db.execute('UPDATE web_jobs SET chat_revision=%s WHERE id=%s', (revision, key))

    def _public(self, owner, key=None):
        # One owned snapshot for both list and detail. Never fetch input payloads
        # or artifact bytes just to render status, files, budgets and queue order.
        with self.store.db() as db:
            db.row_factory = dict_row
            rows = db.execute(
                """WITH selected AS (
                    SELECT id,owner,status,progress,created,engine_job,kind,document_result FROM web_jobs
                    WHERE owner=%s AND (%s::text IS NULL OR id=%s)
                    ORDER BY created DESC LIMIT 100
                )
                SELECT w.*,j.result,b.step_limit,COALESCE(m.attempts,0) AS attempts,
                    COALESCE(f.files,'[]'::json) AS files
                FROM selected w
                LEFT JOIN jobs j ON j.id=w.engine_job AND j.owner=w.owner
                LEFT JOIN job_budgets b ON b.job=j.id AND b.owner=w.owner
                LEFT JOIN model_attempts m ON m.job=j.id
                LEFT JOIN LATERAL (
                    SELECT json_agg(json_build_object(
                        'id',a.id,'name',a.name,'sha256',a.sha,
                        'calendar_preview',c.artifact IS NOT NULL
                    )) AS files
                    FROM artifacts a LEFT JOIN eip_calendar_sources c
                        ON c.owner=a.owner AND c.job=a.job AND c.artifact=a.id
                    WHERE a.owner=w.owner AND a.job=j.id
                ) f ON true
                ORDER BY w.created DESC""", (owner, key, key),
            ).fetchall()
        return [self.public_view(row) for row in rows]

    @staticmethod
    def public_view(row):
        from company_agent.state import GRAPH_VERSION

        value = {k: row[k] for k in ('id', 'status', 'progress', 'created', 'kind')}
        value.update(files=[], proposals=[], answer='')
        if row['status'] == 'queued':
            value['progress'] = '等待執行（多人並行排程，同一帳號依序處理）'
        if row['kind'] == 'document':
            result = json.loads(row['document_result'])
            value.update(answer=result.get('answer', ''), files=result.get('files', []), resumable=False)
            return value
        if row['engine_job']:
            if row['result'] is None:
                raise ValueError('工作不存在或沒有權限。')
            result = json.loads(row['result'])
            for key in (
                'proposals', 'answer', 'tool_feedback', 'delegations', 'usage',
                'notices', 'web_sources', 'budget', 'resumable',
            ):
                value[key] = result.get(key, '' if key == 'answer' else [])
            value['requires_restart'] = result.get('graph_version', GRAPH_VERSION) != GRAPH_VERSION
            if value['requires_restart']:
                value['resumable'] = False
                value['notices'] = [*value['notices'], '此工作建立於更新前，請另開新工作繼續；原有成果仍可下載。']
            if row['step_limit'] is not None:
                value['budget'] = {
                    **result.get('budget', {}), 'used': row['attempts'],
                    'limit': row['step_limit'], 'maximum': MAX_STEPS,
                }
            value['files'] = [{**f, 'url': f'/agents/artifacts/{f["id"]}'} for f in row['files']]
        return value

    def update(self, key, **values):
        if not values or set(values) - {'status', 'progress', 'engine_job', 'payload', 'document_result'}:
            raise ValueError('工作更新格式錯誤。')
        with self.store.db() as db:
            db.execute(
                'UPDATE web_jobs SET ' + ','.join(k + '=%s' for k in values) + ' WHERE id=%s',
                (*values.values(), key),
            )

    def claim(self, excluded_owners=(), kinds=('company', 'document')):
        with self.store.db() as db:
            self.store.lock(db)
            db.row_factory = dict_row
            row = db.execute(
                """SELECT w.* FROM web_jobs w WHERE w.status='queued'
                AND NOT(w.owner=ANY(%s)) AND w.kind=ANY(%s) AND NOT w.cancel_requested
                AND NOT EXISTS(SELECT 1 FROM web_jobs busy WHERE busy.owner=w.owner AND busy.status='running')
                AND NOT EXISTS(SELECT 1 FROM web_jobs earlier WHERE earlier.owner=w.owner
                    AND earlier.status='queued' AND (earlier.created,earlier.id)<(w.created,w.id))
                ORDER BY COALESCE((SELECT max(prior.started) FROM web_jobs prior WHERE prior.owner=w.owner),0),
                    w.created,w.id LIMIT 1 FOR UPDATE OF w SKIP LOCKED""", (list(excluded_owners), list(kinds)),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE web_jobs SET status='running',progress='正在執行',started=%s WHERE id=%s", (time.time(), row['id']),
            )
        row['status'], row['progress'] = 'running', '正在執行'
        row['payload'] = json.loads(row['payload'])
        return dict(row)

    def request_cancel(self, owner, key):
        # Condition and mutation share one transaction, including a concurrent
        # claim. Persist intent if the executor has not yet registered its task.
        with self.store.db() as db:
            return db.execute(
                """UPDATE web_jobs SET cancel_requested=TRUE,
                status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                progress=CASE WHEN status='queued' THEN '已取消' ELSE '正在取消；已產生的成果保留' END
                WHERE owner=%s AND id=%s AND status IN ('queued','running')""", (owner, key),
            ).rowcount == 1

    def recover_running(self):
        with self.store.db() as db:
            db.execute("""UPDATE web_jobs SET
                status=CASE WHEN cancel_requested THEN 'cancelled' ELSE 'interrupted' END,
                progress=CASE WHEN cancel_requested THEN '已取消' ELSE '執行已中斷' END
                WHERE status='running'""")

    def resume(self, owner, subject, key, decision=None):
        row = self.get(owner, key)
        if row['kind'] != 'company':
            raise ValueError('公文工作不自動重播；請另送新訊息重新處理。')
        if row['subject'] != subject:
            raise ValueError('計費身分已變更，不能恢復。')
        if row['status'] in ('queued', 'running'):
            return
        if not row['engine_job']:
            raise ValueError('此工作尚無可恢復節點，請另送新工作。')
        current = self.store.result(owner, row['engine_job'])
        from company_agent.state import GRAPH_VERSION
        if current.get('graph_version', GRAPH_VERSION) != GRAPH_VERSION:
            raise ValueError('此工作建立於更新前，請另開新工作；原有成果仍可下載。')
        if not current.get('resumable'):
            raise ValueError('此工作已結束，請使用接續修改建立新工作。')
        if row['status'] == 'awaiting_approval':
            if (
                not isinstance(decision, dict)
                or set(decision) != {'proposal_id', 'decision'}
                or decision['decision'] not in ('approve', 'reject')
            ):
                raise ValueError('請明確核准或拒絕待確認提案。')
            if decision['proposal_id'] not in {
                p['proposal_id'] for p in current.get('proposals', [])
            }:
                raise ValueError('提案已失效。')
        elif decision is not None:
            raise ValueError('此工作沒有等待中的存檔提案。')
        payload = {**row['payload'], 'operation': 'resume', 'decision': decision}
        with self.store.db() as db:
            self.store.lock(db)
            self.check_capacity(db, owner)
            cursor = db.execute(
                "UPDATE web_jobs SET payload=%s,status='queued',progress='等待恢復',cancel_requested=FALSE WHERE id=%s AND owner=%s AND status=%s",
                (json.dumps(payload, ensure_ascii=False), key, owner, row['status']),
            )
            if cursor.rowcount != 1:
                raise ValueError('工作狀態已變更，請重新整理。')
