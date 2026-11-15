"""Continuation contracts and a bounded in-memory implementation for unit tests.

Production uses the encrypted PostgreSQL repository. Clock arithmetic belongs
to each repository; callers use ``remaining`` rather than assuming a clock.
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from heapq import nsmallest
from operator import attrgetter
from operator import itemgetter
from threading import RLock
from typing import Annotated
from typing import Generic
from typing import TypeVar
from typing import Protocol

from pydantic import ConfigDict
from pydantic import Field
from pydantic import StringConstraints
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass as validated_dataclass

from .output_receipts import OutputReceipts

PayloadT = TypeVar('PayloadT')
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveFiniteSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
FINGERPRINT = TypeAdapter(NonEmptyText, config=ConfigDict(strict=True))
TTL = TypeAdapter(PositiveFiniteSeconds, config=ConfigDict(strict=True))


@validated_dataclass(frozen=True, config=ConfigDict(strict=True))
class PendingClarificationScope:
    """Identity boundary for one pending clarification.

    The caller should construct a scope only when Open WebUI supplied a stable
    non-empty chat identifier. A chat belongs to its user on the server, so
    this continues to work after a browser reconnect while still preventing a
    reply from another conversation from consuming the record. Falling back
    to a user-only key would not provide that boundary.
    """

    user_id: NonEmptyText
    chat_id: NonEmptyText


@dataclass(frozen=True)
class PendingClarification(Generic[PayloadT]):
    """One opaque, server-created clarification payload."""

    scope: PendingClarificationScope
    attachment_fingerprint: str
    payload: PayloadT
    created_at: float
    expires_at: float
    generation: int


class PendingRepository(Protocol[PayloadT]):
    outputs: OutputReceipts

    def review(
        self, scope: PendingClarificationScope
    ) -> PendingClarification[PayloadT] | None: ...

    def put(
        self,
        scope: PendingClarificationScope,
        *,
        attachment_fingerprint: str,
        payload: PayloadT,
        ttl_seconds: float,
    ) -> PendingClarification[PayloadT]: ...
    def get(
        self, scope: PendingClarificationScope
    ) -> PendingClarification[PayloadT] | None: ...
    def claim(
        self,
        scope: PendingClarificationScope,
        *,
        attachment_fingerprint: str,
        expected_generation: int | None = None,
    ) -> PendingClarification[PayloadT] | None: ...
    def restore(self, record: PendingClarification[PayloadT]) -> bool: ...
    def advance(
        self, record: PendingClarification[PayloadT], payload: PayloadT
    ) -> bool: ...
    def finalize(self, record: PendingClarification[PayloadT]) -> bool: ...
    def discard(
        self,
        scope: PendingClarificationScope,
        *,
        expected_generation: int | None = None,
    ) -> bool: ...
    def remaining(self, record: PendingClarification[PayloadT]) -> float: ...


class PendingClarificationStore(Generic[PayloadT]):
    """Bounded process-local store with explicit claim semantics.

    ``claim`` is one-shot, so two concurrent follow-up requests cannot both
    apply the same calendar operation.  If a caller fails after claiming a
    record but before producing its output, it can use ``restore`` while the
    original expiry remains valid.
    """

    def __init__(
        self,
        *,
        max_entries: int = 64,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError('max_entries must be positive')
        self._max_entries = max_entries
        self._clock = clock
        self._lock = RLock()
        from .output_receipts import MemoryOutputReceipts

        self.outputs = MemoryOutputReceipts()
        self._claimed = {}
        self._records: dict[
            PendingClarificationScope, PendingClarification[PayloadT]
        ] = {}
        # A generation survives an in-flight ``claim`` until the record's
        # original expiry.  It prevents a later ``restore`` from reviving
        # state which a new upload deliberately invalidated meanwhile.
        self._generations: dict[PendingClarificationScope, tuple[int, float]] = {}
        # This is process-local rather than per-scope on purpose.  A marker
        # can be trimmed under load; a later record for that same scope must
        # still never reuse the old token and make a stale ``restore`` match.
        self._next_generation = 0

    def _new_generation_locked(self) -> int:
        self._next_generation += 1
        return self._next_generation

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            scope for scope, record in self._records.items() if record.expires_at <= now
        ]
        for scope in expired:
            del self._records[scope]
        expired_generations = [
            scope
            for scope, (_generation, expires_at) in self._generations.items()
            if expires_at <= now
        ]
        for scope in expired_generations:
            self._claimed.pop(scope, None)
            del self._generations[scope]

    def _evict_oldest_locked(self) -> None:
        oldest = min(self._records.values(), key=attrgetter('expires_at')).scope
        del self._records[oldest]
        # Stored records have not been claimed, so no caller can restore them.
        self._generations.pop(oldest, None)

    def _trim_generation_markers_locked(self) -> None:
        """Bound metadata left by in-flight claims as well as record payloads.

        A marker is only needed to make a later ``restore`` safe.  If an
        overloaded process has more in-flight claims than this conservative
        limit, dropping the oldest marker can merely turn a later retry into a
        user-visible re-answer; it can never revive stale state.  Stored
        records are not candidates because they already count against
        ``max_entries``.
        """

        excess = len(self._generations) - self._max_entries * 2
        if excess <= 0:
            return
        claimed = (
            (value[1], scope)
            for scope, value in self._generations.items()
            if scope not in self._records
        )
        for _expires, scope in nsmallest(excess, claimed, key=itemgetter(0)):
            self._claimed.pop(scope, None)
            del self._generations[scope]

    def put(
        self,
        scope: PendingClarificationScope,
        *,
        attachment_fingerprint: str,
        payload: PayloadT,
        ttl_seconds: float,
    ) -> PendingClarification[PayloadT]:
        """Replace the pending record for a scope and return the new record."""

        fingerprint = FINGERPRINT.validate_python(attachment_fingerprint)
        ttl = TTL.validate_python(ttl_seconds)
        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            expires_at = now + ttl
            if scope not in self._records and len(self._records) >= self._max_entries:
                self._evict_oldest_locked()
            generation = self._new_generation_locked()
            record = PendingClarification(
                scope=scope,
                attachment_fingerprint=fingerprint,
                payload=payload,
                created_at=now,
                expires_at=expires_at,
                generation=generation,
            )
            self._claimed.pop(scope, None)
            self._records[scope] = record
            self._generations[scope] = (generation, expires_at)
            self._trim_generation_markers_locked()
            return record

    def get(
        self,
        scope: PendingClarificationScope,
    ) -> PendingClarification[PayloadT] | None:
        """Read a non-expired record without consuming it.

        This exists so an adapter can revalidate the payload's source-file
        ownership and recompute its attachment fingerprint before calling
        :meth:`claim`.
        """

        with self._lock:
            self._purge_expired_locked(self._clock())
            return self._records.get(scope)

    def claim(
        self,
        scope: PendingClarificationScope,
        *,
        attachment_fingerprint: str,
        expected_generation: int | None = None,
    ) -> PendingClarification[PayloadT] | None:
        """Atomically consume a record only when its attachment hash matches."""

        fingerprint = FINGERPRINT.validate_python(attachment_fingerprint)
        with self._lock:
            self._purge_expired_locked(self._clock())
            record = self._records.get(scope)
            if record is None or (
                expected_generation is not None
                and record.generation != expected_generation
            ):
                return None
            if not hmac.compare_digest(
                record.attachment_fingerprint.encode('utf-8'),
                fingerprint.encode('utf-8'),
            ):
                return None
            self._claimed[scope] = record
            return self._records.pop(scope)

    def restore(self, record: PendingClarification[PayloadT]) -> bool:
        """Restore a claimed record only if it is still valid and not replaced."""

        with self._lock:
            now = self._clock()
            self._purge_expired_locked(now)
            generation = self._generations.get(record.scope)
            if (
                record.expires_at <= now
                or record.scope in self._records
                or generation is None
                or generation[0] != record.generation
            ):
                return False
            if len(self._records) >= self._max_entries:
                self._evict_oldest_locked()
            self._claimed.pop(record.scope, None)
            self._records[record.scope] = record
            self._trim_generation_markers_locked()
            return True

    def finalize(self, record: PendingClarification[PayloadT]) -> bool:
        """Release a successfully claimed record's generation marker.

        If a new upload or new pending record has already superseded the
        generation, it is left untouched.
        """

        with self._lock:
            self._purge_expired_locked(self._clock())
            generation = self._generations.get(record.scope)
            if (
                record.scope in self._records
                or generation is None
                or generation[0] != record.generation
            ):
                return False
            self._claimed.pop(record.scope, None)
            self._generations.pop(record.scope, None)
            return True

    def discard(
        self, scope: PendingClarificationScope, *, expected_generation=None
    ) -> bool:
        """Discard a scope, such as when a new attachment batch supersedes it."""

        with self._lock:
            self._purge_expired_locked(self._clock())
            generation = self._generations.get(scope)
            if expected_generation is not None and (
                generation is None or generation[0] != expected_generation
            ):
                return False
            self._claimed.pop(scope, None)
            record = self._records.pop(scope, None)
            if generation is not None:
                # Preserve the original expiry but invalidate an in-flight
                # claim. ``restore`` can now succeed only for a newer record.
                self._generations[scope] = (
                    self._new_generation_locked(),
                    generation[1],
                )
            return record is not None

    def __len__(self) -> int:
        with self._lock:
            self._purge_expired_locked(self._clock())
            return len(self._records)

    def review(self, scope):
        with self._lock:
            self._purge_expired_locked(self._clock())
            record = self._claimed.get(scope)
            marker = self._generations.get(scope)
            return (
                record if record and marker and marker[0] == record.generation else None
            )

    def remaining(self, record):
        return record.expires_at - self._clock()

    def advance(self, record, payload):
        from dataclasses import replace

        return self.restore(replace(record, payload=payload))
