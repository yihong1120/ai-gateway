"""Durable owner/client boundaries and source-version snapshots; never secrets."""
from __future__ import annotations

import hashlib
import json
import time
import uuid


class ResourceError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Repository:
    def __init__(self, store):
        self.store = store

    def grant(self, person, *, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.grant(person, db=connection)
        row = db.execute(
            'SELECT enabled,revision,updated FROM mcp_grants WHERE owner=%s AND client=%s',
            (person.owner, person.client),
        ).fetchone()
        if row is None:
            db.execute(
                'INSERT INTO mcp_grants VALUES(%s,%s,TRUE,%s,%s) ON CONFLICT DO NOTHING',
                (person.owner, person.client, uuid.uuid4().hex, time.time()),
            )
            row = db.execute(
                'SELECT enabled,revision,updated FROM mcp_grants WHERE owner=%s AND client=%s',
                (person.owner, person.client),
            ).fetchone()
        if not row[0]:
            raise ResourceError('grant_revoked', '公司 MCP 存取已撤回，請在帳號與連線重新授權。')
        if row[2] + 30 * 86400 <= time.time():
            raise ResourceError('grant_expired', '公司資源授權已到期，請在帳號與連線恢復授權。')
        return row[1]

    def set_grant(self, owner, client, enabled):
        with self.store.db() as db:
            self.store.lock(db)
            db.execute(
                'INSERT INTO mcp_grants VALUES (%s,%s,%s,%s,%s) ON CONFLICT(owner,client) '
                'DO UPDATE SET enabled=excluded.enabled,revision=excluded.revision,updated=excluded.updated',
                (owner, client, enabled, uuid.uuid4().hex, time.time()),
            )
            # Even a revoke/re-enable cycle invalidates every old workspace.

    def grants(self, owner, clients):
        with self.store.db() as db:
            rows = {
                client: (enabled and updated + 30 * 86400 > time.time()) for client, enabled, updated in
                db.execute('SELECT client,enabled,updated FROM mcp_grants WHERE owner=%s', (owner,))
            }
        return [{'client_id': client, 'enabled': rows.get(client, True)} for client in clients]

    def rate_limit(self, owner):
        with self.store.db() as db:
            window = int(time.time()) // 60
            count = db.execute(
                'INSERT INTO mcp_request_limits VALUES(%s,%s,1) ON CONFLICT(owner) DO UPDATE SET '
                'window_start=excluded.window_start,attempts=CASE WHEN mcp_request_limits.window_start=excluded.window_start '
                'THEN mcp_request_limits.attempts+1 ELSE 1 END RETURNING attempts', (owner, window),
            ).fetchone()[0]
        if count > 60:
            raise ResourceError('rate_limited', '公司資源查詢過於頻繁，請稍後再試。')

    def create_workspace(self, person, snapshot, duration, request_id):
        with self.store.db() as db:
            self.store.lock(db)
            revision = self.grant(person, db=db)
            now = time.time()
            # One request ID gives the same workspace, including after the
            # caller loses the HTTP response; never silently rebind it.
            identifier = hashlib.sha256(
                json.dumps(
                    [person.owner, person.client, request_id], separators=(',', ':'),
                ).encode(),
            ).hexdigest()
            old = db.execute('SELECT id FROM mcp_workspaces WHERE id=%s', (identifier,)).fetchone()
            if old:
                return identifier
            count = db.execute(
                'SELECT count(*) FROM mcp_workspaces WHERE owner=%s AND expires>%s', (person.owner, now),
            ).fetchone()[0]
            if count >= 20:
                raise ResourceError('workspace_limit', '本人的資源工作空間過多，請稍後建立。')
            db.execute(
                'INSERT INTO jobs(id,owner,status,result,created) VALUES(%s,%s,%s,%s,%s)',
                (identifier, person.owner, 'running', '{}', now),
            )
            db.execute(
                'INSERT INTO mcp_workspaces VALUES(%s,%s,%s,%s,%s,%s,%s)',
                (
                    identifier, person.owner, person.subject, person.client, revision,
                    json.dumps(snapshot), now + duration,
                ),
            )
        return identifier

    def workspace(self, person, identifier, *, allow_expired=False):
        with self.store.db() as db:
            revision = self.grant(person, db=db)
            row = db.execute(
                'SELECT grant_revision,snapshot,expires FROM mcp_workspaces '
                'WHERE id=%s AND owner=%s AND subject=%s AND client=%s',
                (identifier, person.owner, person.subject, person.client),
            ).fetchone()
        if not row or not allow_expired and row[2] <= time.time():
            raise ResourceError('workspace_expired', '工作空間不存在、已過期或不屬於目前帳號。')
        if row[0] != revision:
            raise ResourceError('grant_changed', '存取授權已變更，請建立新工作空間。')
        return json.loads(row[1])

    def prepare(self, person, workspace, request_id, payload):
        self.workspace(person, workspace)
        digest = hashlib.sha256(json.dumps([workspace, payload], sort_keys=True).encode()).hexdigest()
        with self.store.db() as db:
            self.store.lock(db)
            row = db.execute(
                'SELECT id,digest FROM mcp_operations WHERE owner=%s AND client=%s AND request_id=%s',
                (person.owner, person.client, request_id),
            ).fetchone()
            if row:
                if row[1] != digest:
                    raise ResourceError('request_changed', '此請求識別已用於其他內容，請另建操作。')
                return row[0]
            count = db.execute(
                'SELECT count(*) FROM mcp_operations o WHERE owner=%s AND web_job IS NULL '
                'AND EXISTS(SELECT 1 FROM mcp_workspaces w WHERE w.id=o.workspace AND w.expires>%s)',
                (person.owner, time.time()),
            ).fetchone()[0]
            if count >= 20:
                raise ResourceError('operation_limit', '待確認操作過多，請先處理既有操作。')
            identifier = uuid.uuid4().hex
            db.execute(
                'INSERT INTO mcp_operations VALUES(%s,%s,%s,%s,%s,%s,%s,NULL,%s)',
                (
                    identifier, person.owner, person.client, workspace, request_id, digest,
                    json.dumps(payload, ensure_ascii=False), time.time(),
                ),
            )
        return identifier

    def operation(self, owner, identifier, *, client=None):
        with self.store.db() as db:
            row = db.execute(
                'SELECT client,workspace,payload,web_job FROM mcp_operations WHERE owner=%s AND id=%s',
                (owner, identifier),
            ).fetchone()
        if not row or client is not None and row[0] != client:
            raise ResourceError('operation_unavailable', '操作不存在或不屬於目前帳號。')
        return {'client': row[0], 'workspace': row[1], 'payload': json.loads(row[2]), 'web_job': row[3]}

    def submitted(self, owner, identifier, web_job):
        with self.store.db() as db:
            db.execute(
                'UPDATE mcp_operations SET web_job=%s WHERE owner=%s AND id=%s',
                (web_job, owner, identifier),
            )

    def submit_analysis(self, person, identifier, queue):
        operation = self.operation(person.owner, identifier)
        snapshot = self.workspace(person, operation['workspace'])
        if operation['client'] != person.client:
            raise ResourceError('operation_unavailable', '操作不屬於目前用戶端。')
        with self.store.db() as db:
            self.store.lock(db)
            self.grant(person, db=db)
            old = db.execute(
                'SELECT web_job FROM mcp_operations WHERE owner=%s AND id=%s FOR UPDATE',
                (person.owner, identifier),
            ).fetchone()
            if old[0]:
                return old[0]  # Includes failed/interrupted jobs: never auto-replay.
            queue.check_capacity(db, person.owner)
            payload = {
                **operation['payload'], 'operation': 'mcp_analysis',
                'workspace': operation['workspace'], 'mcp_client': person.client,
                'nas_binding_revisions': snapshot['nas_revisions'],
                'eip_binding_revision': snapshot['eip_generation'],
            }
            web_job = str(uuid.uuid4())
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            db.execute(
                'INSERT INTO web_jobs(id,owner,subject,chat,request_id,request_hash,status,payload,progress,created,kind) '
                "VALUES(%s,%s,%s,%s,%s,%s,'queued',%s,'本人已確認，等待公文分析',%s,'mcp_analysis')",
                (
                    web_job, person.owner, person.subject, 'mcp:' + person.client, identifier, digest,
                    json.dumps(payload, ensure_ascii=False), time.time(),
                ),
            )
            db.execute(
                'UPDATE mcp_operations SET web_job=%s WHERE owner=%s AND id=%s',
                (web_job, person.owner, identifier),
            )
        return web_job
