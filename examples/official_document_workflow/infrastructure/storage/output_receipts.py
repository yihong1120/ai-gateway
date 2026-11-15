"""Encrypted durable receipts; they never expire with a pending conversation."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from dataclasses import replace

from examples.official_document_workflow.application.output_receipts import OutputReceipt
from examples.official_document_workflow.application.output_receipts import require_same_output
from examples.official_document_workflow.application.output_receipts import ReviewClaim
from examples.official_document_workflow.application.output_receipts import validate_review_transition


class PostgresOutputReceipts:
    def __init__(self, store_factory):
        self._store = store_factory

    def _record(self, store, row):
        if row is None:
            return None
        payload = store.decode(row['owner'], row['payload'])
        return OutputReceipt(
            row['id'],
            row['owner'],
            row['state'],
            payload['name'],
            payload['sha256'],
            payload['source'],
            payload.get('kind', 'calendar_csv_pending_reminder'),
            payload.get('job'), payload.get('size', 0), payload.get('path'),
            row['revision'], row['projection_pending'], row['review_required'],
        )

    def get(self, key, owner):
        store = self._store()
        with store.db() as db:
            row = db.execute(
                'SELECT * FROM clarification_outputs WHERE id=%s AND owner=%s',
                (key, owner),
            ).fetchone()
        return self._record(store, row)

    def begin(self, key, owner, name, digest, source, *, kind='calendar_csv_pending_reminder', job=None, size=0):
        store = self._store()
        payload = store.encode(
            owner, asdict(OutputReceipt(key, owner, 'writing', name, digest, source, kind, job, size)),
        )
        with store.db() as db:
            row = db.execute(
                """INSERT INTO clarification_outputs(id,owner,state,payload,updated,job_id,kind,next_review)
                VALUES(%s,%s,'writing',%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE
                SET state='writing',payload=excluded.payload,updated=excluded.updated,job_id=excluded.job_id,kind=excluded.kind,
                    revision=clarification_outputs.revision+1,next_review=excluded.next_review,
                    review_until=0,review_token=NULL,review_failures=0,projection_pending=FALSE,
                    review_required=FALSE,review_outcome=NULL,review_reason=NULL,mismatch_count=0
                WHERE clarification_outputs.owner=excluded.owner AND clarification_outputs.state='not_written'
                RETURNING *""",
                (key, owner, payload, time.time(), job, kind, time.time()+30),
            ).fetchone()
            started = row is not None
            if row is None:
                row = db.execute(
                    'SELECT * FROM clarification_outputs WHERE id=%s AND owner=%s',
                    (key, owner),
                ).fetchone()
            if row is None:
                raise ValueError('Output receipt ownership mismatch.')
            return self._record(store, row), started

    def mark(self, record, state):
        if state not in {'stored', 'not_written', 'unknown'}:
            raise ValueError('Invalid output transition.')
        store = self._store()
        with store.db() as db:
            row = db.execute(
                """UPDATE clarification_outputs SET state=%s,updated=%s,revision=revision+1,
                    projection_pending=(job_id IS NOT NULL AND %s='stored'),
                    review_required=CASE WHEN %s='stored' THEN FALSE ELSE review_required END,
                    next_review=CASE WHEN %s='stored' THEN 0 ELSE %s END
                WHERE id=%s AND owner=%s AND revision=%s AND state IN ('writing','unknown') RETURNING *""",
                (state, time.time(), state, state, state, time.time()+30, record.id, record.owner, record.revision),
            ).fetchone()
            if row is None:
                row = db.execute(
                    'SELECT * FROM clarification_outputs WHERE id=%s AND owner=%s',
                    (record.id, record.owner),
                ).fetchone()
        current = self._record(store, row)
        if current:
            require_same_output(current, record)
        return current

    def list(self, owner, *, state=None, limit=50):
        if not owner or not 1 <= limit <= 100 or state not in {None, 'writing', 'unknown', 'not_written', 'stored', 'deleting', 'delete_unknown', 'deleted'}:
            raise ValueError('Invalid receipt selection.')
        store = self._store()
        with store.db() as db:
            rows = db.execute(
                """SELECT id,state,updated,review_required,review_outcome,review_reason,mismatch_count,review_evidence FROM clarification_outputs
                WHERE owner=%s AND (%s::text IS NULL OR state=%s)
                ORDER BY updated DESC,id DESC LIMIT %s""", (owner, state, state, limit),
            ).fetchall()
        return [
            {
                **{key: value for key, value in row.items() if key != 'review_evidence'},
                'proof': store.decode(owner, row['review_evidence']) if row['review_evidence'] else {},
            } for row in rows
        ]

    def review_status(self, record):
        store = self._store()
        with store.db() as db:
            row = db.execute(
                """SELECT review_required,review_outcome,review_reason,mismatch_count,
                review_evidence,updated FROM clarification_outputs WHERE id=%s AND owner=%s""",
                (record.id, record.owner),
            ).fetchone()
        if row is None:
            return None
        return {
            **{key: value for key, value in row.items() if key != 'review_evidence'},
            'proof': store.decode(record.owner, row['review_evidence']) if row['review_evidence'] else {},
        }

    def reviews(self, key, owner, *, limit=50):
        if not 1 <= limit <= 100:
            raise ValueError('Invalid review limit.')
        store = self._store()
        with store.db() as db:
            rows = db.execute(
                """SELECT * FROM clarification_output_reviews
                WHERE output_id=%s AND owner=%s ORDER BY id DESC LIMIT %s""", (key, owner, limit),
            ).fetchall()
        return [
            {
                **{k: row[k] for k in ('id', 'actor', 'method', 'outcome', 'created')},
                **store.decode(owner, row['payload']),
            } for row in rows
        ]

    def record_review(self, record, *, actor, method, outcome, note='', proof=None):
        allowed = {
            'manual': {'needs_review', 'reported_missing', 'investigating'},
            'webui_sha256': {'verified', 'not_found', 'mismatch', 'unavailable'},
        }
        if (
            method not in allowed or outcome not in allowed[method] or not actor.strip()
            or len(actor) > 200 or any(c in actor for c in '\r\n\0') or len(note) > 2000
        ):
            raise ValueError('Invalid review metadata.')
        if method == 'webui_sha256' and outcome == 'verified':
            if (
                not isinstance(proof, dict) or proof.get('sha256') != record.sha256
                or type(proof.get('bytes')) is not int or not 0 <= proof['bytes'] <= (record.size or 20_000_000)
            ):
                raise ValueError('Verified reviews require the matching content hash and byte count.')
        store = self._store()
        payload = store.encode(record.owner, {'note': note, 'proof': proof or {}})
        with store.db() as db:
            row = db.execute(
                'SELECT * FROM clarification_outputs WHERE id=%s AND owner=%s FOR UPDATE',
                (record.id, record.owner),
            ).fetchone()
            current = self._record(store, row)
            if current is None:
                raise ValueError('Receipt no longer exists.')
            require_same_output(current, record)
            if method == 'webui_sha256' and current.state in {'deleting', 'delete_unknown', 'deleted'}:
                raise ValueError('Receipt changed during verification; inspect it again.')
            # Finding no file is never proof that an earlier write rolled back.
            # Manual annotations cannot reopen a writer or mark it successful.
            if method == 'webui_sha256' and outcome == 'verified':
                db.execute(
                    """UPDATE clarification_outputs SET state='stored',updated=%s,revision=revision+1,
                    projection_pending=(job_id IS NOT NULL),next_review=0,review_required=FALSE,
                    review_outcome='verified',review_reason=NULL,mismatch_count=0 WHERE id=%s AND owner=%s""",
                    (time.time(), record.id, record.owner),
                )
            return db.execute(
                """INSERT INTO clarification_output_reviews(output_id,owner,actor,method,outcome,payload,created)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (record.id, record.owner, actor, method, outcome, payload, time.time()),
            ).fetchone()['id']

    def for_jobs(self, owner, jobs):
        if not jobs:
            return []
        store = self._store()
        with store.db() as db:
            rows = db.execute(
                "SELECT * FROM clarification_outputs WHERE owner=%s AND job_id=ANY(%s) AND state IN ('stored','deleting','delete_unknown','deleted')",
                (owner, list(jobs)),
            ).fetchall()
        return [self._record(store, row) for row in rows]

    def request_delete(self, record, path):
        store = self._store()
        payload = store.encode(record.owner, asdict(replace(record, path=path)))
        with store.db() as db:
            row = db.execute(
                """UPDATE clarification_outputs SET state='deleting',payload=%s,
                    revision=revision+1,updated=%s,next_review=%s,projection_pending=(job_id IS NOT NULL),
                    review_token=NULL,review_until=0,review_required=FALSE,mismatch_count=0
                WHERE id=%s AND owner=%s AND revision=%s
                  AND state IN ('writing','unknown','stored','deleting','delete_unknown') RETURNING *""",
                (payload, time.time(), time.time()+30, record.id, record.owner, record.revision),
            ).fetchone()
        return self._record(store, row)

    def finish_delete(self, record):
        store = self._store()
        with store.db() as db:
            row = db.execute(
                """UPDATE clarification_outputs SET state='deleted',revision=revision+1,
                    updated=%s,projection_pending=(job_id IS NOT NULL),next_review=0
                WHERE id=%s AND owner=%s AND revision=%s AND state IN ('deleting','delete_unknown') RETURNING *""",
                (time.time(), record.id, record.owner, record.revision),
            ).fetchone()
        return self._record(store, row)

    def claim_reviews(self, limit=32):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Output review batch must be 1–100.')
        store, now, token = self._store(), time.time(), str(uuid.uuid4())
        with store.db() as db:
            rows = db.execute(
                """WITH due AS (
                SELECT id FROM clarification_outputs
                WHERE next_review<=%s AND review_until<=%s
                  AND ((NOT review_required AND state IN ('writing','unknown','deleting','delete_unknown')) OR projection_pending)
                ORDER BY next_review,updated,id LIMIT %s FOR UPDATE SKIP LOCKED
            ) UPDATE clarification_outputs o SET review_until=%s,review_token=%s FROM due
                WHERE o.id=due.id RETURNING o.*""", (now, now, limit, now+60, token),
            ).fetchall()
        return [ReviewClaim(self._record(store, row), token) for row in rows]

    def renew_review(self, claim):
        store, record, now = self._store(), claim.record, time.time()
        with store.db() as db:
            return db.execute(
                """UPDATE clarification_outputs SET review_until=%s
                WHERE id=%s AND owner=%s AND revision=%s AND review_token=%s AND review_until>%s""",
                (now+60, record.id, record.owner, record.revision, claim.token, now),
            ).rowcount == 1

    def resolve_review(self, claim, state, *, verification=None):
        validate_review_transition(claim.record.state, state)
        store, record = self._store(), claim.record
        outcome = verification.outcome if verification is not None else None
        reason = verification.reason if verification is not None else None
        evidence = store.encode(record.owner, verification.proof) if verification is not None else None
        with store.db() as db:
            row = db.execute(
                """UPDATE clarification_outputs SET state=%s,revision=revision+1,updated=%s,
                    projection_pending=(projection_pending OR (job_id IS NOT NULL AND %s IN ('stored','deleted'))),
                    review_outcome=%s,review_reason=%s,review_evidence=%s,
                    mismatch_count=CASE WHEN %s='mismatch' THEN mismatch_count+1 ELSE 0 END,
                    review_required=(COALESCE(%s='mismatch',FALSE) AND mismatch_count+1>=3)
                WHERE id=%s AND owner=%s AND revision=%s AND review_token=%s AND review_until>%s
                RETURNING *""", (
                    state, time.time(), state, outcome, reason, evidence, outcome, outcome,
                    record.id, record.owner, record.revision, claim.token, time.time(),
                ),
            ).fetchone()
        return ReviewClaim(self._record(store, row), claim.token) if row else None

    def finish_review(self, claim, *, published=False):
        store, record, now = self._store(), claim.record, time.time()
        with store.db() as db:
            return db.execute(
                """UPDATE clarification_outputs SET
                projection_pending=(projection_pending AND NOT %s),review_token=NULL,review_until=0,
                next_review=CASE WHEN (NOT review_required AND state IN ('writing','unknown','deleting','delete_unknown'))
                    OR (projection_pending AND NOT %s) THEN %s+LEAST(300,power(2,LEAST(review_failures+1,9))) ELSE 0 END,
                review_failures=CASE WHEN (NOT review_required AND state IN ('writing','unknown','deleting','delete_unknown'))
                    OR (projection_pending AND NOT %s) THEN review_failures+1 ELSE 0 END
                WHERE id=%s AND owner=%s AND revision=%s AND review_token=%s AND review_until>%s""",
                (published, published, now, published, record.id, record.owner, record.revision, claim.token, now),
            ).rowcount == 1
