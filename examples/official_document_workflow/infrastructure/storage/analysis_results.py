"""Durable paid analysis, shared only after each consumer authorizes its source.

Ready records survive cache expiry and process restarts. Unknown paid starts
are never retried automatically, including corrupt/missing result publication.
The inherited PostgreSQL lock coordinates independent application workers.
"""
from __future__ import annotations

import hashlib

from .shared_content import _MISS
from .shared_content import SharedContentCache
from examples.official_document_workflow.domain.documents import ProcessingError


ANALYSIS_CONTRACT = 'document-analysis-v1'


class AnalysisResults(SharedContentCache):
    def __init__(self, store, scope):
        super().__init__(store, scope, ANALYSIS_CONTRACT)

    def get_or_compute(self, key, factory, validate=None, *, replay_safe=False):
        return super().get_or_compute(key, factory, validate, replay_safe=replay_safe)

    async def aget_or_compute(self, key, async_factory, validate=None, *, replay_safe=False):
        return await super().aget_or_compute(key, async_factory, validate, replay_safe=replay_safe)

    @staticmethod
    def _query():
        return """SELECT state,revision,payload FROM document_analysis_results
            WHERE scope=%s AND key=%s AND (state IN ('ready','unknown') OR expires_at>CURRENT_TIMESTAMP)"""

    @staticmethod
    def _upsert():
        return """INSERT INTO document_analysis_results(scope,key,revision,state,payload,created_at,expires_at)
            VALUES(%s,%s,%s,%s,%s,CURRENT_TIMESTAMP,
                CASE WHEN %s IN ('ready','unknown') THEN 'infinity'::timestamptz
                     ELSE CURRENT_TIMESTAMP + %s * INTERVAL '1 second' END)
            ON CONFLICT(scope,key) DO UPDATE SET revision=excluded.revision,state=excluded.state,
                payload=excluded.payload,created_at=excluded.created_at,expires_at=excluded.expires_at"""

    def _decode(self, key, row, validate):
        value = super()._decode(key, row, validate)
        if row is not None and row['state'] == 'ready' and value is _MISS:
            raise ProcessingError('既有公文判讀紀錄未通過驗證，需人工核對；未重新付費判讀。')
        return value

    def record_use(self, key, owner, job):
        if not owner:
            return
        with self.store.db() as db:
            db.execute(
                """INSERT INTO document_analysis_uses(scope,key,owner,job) VALUES(%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""", (self.scope, key, owner, job or ''),
            )

    def confirm_not_started(self, key, *, actor, reason):
        """Explicit operator evidence that no provider call started, never a retry on timeout."""
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str) or not reason.strip():
            raise ValueError('Operator and evidence are required.')
        with self.store.db() as db:
            locked = db.execute(
                'SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',
                (self._lock_name(key),),
            ).fetchone()['locked']
            if not locked:
                raise ValueError('Analysis is still running; its reservation cannot be released.')
            row = db.execute(
                'SELECT state,payload FROM document_analysis_results WHERE scope=%s AND key=%s FOR UPDATE',
                (self.scope, key),
            ).fetchone()
            if row is None or row['state'] != 'unknown':
                raise ValueError('Only an uncertain paid reservation can be reconciled.')
            db.execute(
                """INSERT INTO document_analysis_resolutions(scope,key,actor,reason,previous_payload_sha256)
                VALUES(%s,%s,%s,%s,%s)""", (self.scope, key, actor, reason, hashlib.sha256(bytes(row['payload'])).hexdigest()),
            )
            db.execute(
                """UPDATE document_analysis_results SET state='failed',payload=%s,expires_at=CURRENT_TIMESTAMP
                WHERE scope=%s AND key=%s""", (self._encode(key, None, failed=True), self.scope, key),
            )
