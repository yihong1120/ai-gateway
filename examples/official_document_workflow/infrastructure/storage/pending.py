"""Encrypted PostgreSQL continuations with generation-checked atomic transitions.

Claimed rows remain unavailable after a worker crash: an ambiguous CSV write
must never be silently replayed. A fresh upload replaces the generation, while
an ordinary reconnect can resume any unclaimed continuation until its expiry.
"""

from __future__ import annotations

import time

from pydantic import TypeAdapter

from examples.official_document_workflow.application.pending import (
    FINGERPRINT,
    TTL,
    PendingClarification,
)
from examples.official_document_workflow.domain.documents import PendingCalendarReminder

PAYLOAD = TypeAdapter(PendingCalendarReminder)


class PostgresClarifications:
    def __init__(self, backend=None):
        self._backend = backend
        from .output_receipts import PostgresOutputReceipts

        self.outputs = PostgresOutputReceipts(lambda: self.backend)

    @property
    def backend(self):
        if self._backend is not None:
            return self._backend
        from .automation_store import store
        return store()

    def _record(self, scope, row):
        if row is None:
            return None
        return PendingClarification(
            scope,
            row['fingerprint'],
            PAYLOAD.validate_json(self.backend.decode(scope.user_id, row['payload'])),
            row['created_at'],
            row['expires_at'],
            row['generation'],
        )

    def _encode(self, scope, payload):
        return self.backend.encode(scope.user_id, PAYLOAD.dump_json(payload).decode())

    def put(self, scope, *, attachment_fingerprint, payload, ttl_seconds):
        fingerprint = FINGERPRINT.validate_python(attachment_fingerprint)
        ttl = TTL.validate_python(ttl_seconds)
        encrypted = self._encode(scope, payload)
        with self.backend.db() as db:
            # Expired entries are disposable continuations, never write receipts.
            db.execute("""DELETE FROM pending_clarifications WHERE (owner,chat) IN
                (SELECT owner,chat FROM pending_clarifications
                 WHERE expires_at <= extract(epoch FROM clock_timestamp())
                 ORDER BY expires_at LIMIT 1000 FOR UPDATE SKIP LOCKED)""")
            row = db.execute(
                """
                INSERT INTO pending_clarifications(owner,chat,fingerprint,payload,created_at,expires_at)
                VALUES (%s,%s,%s,%s,extract(epoch FROM clock_timestamp()),extract(epoch FROM clock_timestamp())+%s)
                ON CONFLICT(owner,chat) DO UPDATE SET generation=excluded.generation,
                fingerprint=excluded.fingerprint,payload=excluded.payload,created_at=excluded.created_at,
                expires_at=excluded.expires_at,claimed=FALSE RETURNING *
            """,
                (scope.user_id, scope.chat_id, fingerprint, encrypted, ttl),
            ).fetchone()
        return self._record(scope, row)

    def get(self, scope):
        with self.backend.db() as db:
            row = db.execute(
                """SELECT * FROM pending_clarifications WHERE owner=%s AND chat=%s
                AND NOT claimed AND expires_at > extract(epoch FROM clock_timestamp())""",
                (scope.user_id, scope.chat_id),
            ).fetchone()
        return self._record(scope, row)

    def review(self, scope):
        with self.backend.db() as db:
            row = db.execute(
                """SELECT * FROM pending_clarifications WHERE owner=%s AND chat=%s
                AND claimed AND expires_at > extract(epoch FROM clock_timestamp())""",
                (scope.user_id, scope.chat_id),
            ).fetchone()
        return self._record(scope, row)

    def claim(self, scope, *, attachment_fingerprint, expected_generation=None):
        fingerprint = FINGERPRINT.validate_python(attachment_fingerprint)
        with self.backend.db() as db:
            row = db.execute(
                """UPDATE pending_clarifications SET claimed=TRUE,
                generation=nextval(pg_get_serial_sequence('pending_clarifications','generation'))
                WHERE owner=%s AND chat=%s
                AND fingerprint=%s AND NOT claimed AND expires_at > extract(epoch FROM clock_timestamp())
                AND (%s::bigint IS NULL OR generation=%s) RETURNING *""",
                (
                    scope.user_id,
                    scope.chat_id,
                    fingerprint,
                    expected_generation,
                    expected_generation,
                ),
            ).fetchone()
            # Deserialize before commit so corrupt ciphertext never consumes state.
            return self._record(scope, row)

    def restore(self, record):
        return self.advance(record, record.payload)

    def advance(self, record, payload):
        encrypted = self._encode(record.scope, payload)
        with self.backend.db() as db:
            return (
                db.execute(
                    """UPDATE pending_clarifications SET claimed=FALSE,payload=%s
                WHERE owner=%s AND chat=%s AND generation=%s AND claimed
                AND expires_at > extract(epoch FROM clock_timestamp())""",
                    (
                        encrypted,
                        record.scope.user_id,
                        record.scope.chat_id,
                        record.generation,
                    ),
                ).rowcount
                == 1
            )

    def finalize(self, record):
        with self.backend.db() as db:
            return (
                db.execute(
                    """DELETE FROM pending_clarifications
                WHERE owner=%s AND chat=%s AND generation=%s AND claimed""",
                    (record.scope.user_id, record.scope.chat_id, record.generation),
                ).rowcount
                == 1
            )

    def discard(self, scope, *, expected_generation=None):
        with self.backend.db() as db:
            return (
                db.execute(
                    """DELETE FROM pending_clarifications WHERE owner=%s AND chat=%s
                        AND (%s::bigint IS NULL OR generation=%s)""",
                    (
                        scope.user_id,
                        scope.chat_id,
                        expected_generation,
                        expected_generation,
                    ),
                ).rowcount
                == 1
            )

    @staticmethod
    def remaining(record):
        return record.expires_at - time.time()
