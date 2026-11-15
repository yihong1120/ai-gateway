"""Typed output identities and receipt transitions, with an in-memory test adapter."""
from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Literal, Protocol
import time
import uuid

from examples.official_document_workflow.domain.documents import OutputUncertain

OutputState = Literal['writing', 'not_written', 'unknown', 'stored', 'deleting', 'delete_unknown', 'deleted']
WRITE_PENDING = frozenset({'writing', 'unknown'})
DELETE_PENDING = frozenset({'deleting', 'delete_unknown'})
DELETION_REVIEW_NOTICE = '部分輸出的刪除結果待核對，未重送刪除。'


def require_same_output(receipt, expected):
    fields = ('id', 'owner', 'name', 'sha256', 'source', 'kind')
    if receipt is None or any(getattr(receipt, field) != getattr(expected, field) for field in fields):
        raise OutputUncertain('固定輸出識別的內容已變更，未覆寫或重送。')
    if receipt.size and expected.size and receipt.size != expected.size:
        raise OutputUncertain('輸出大小與收條不符，需核對。')
    if receipt.job and expected.job and receipt.job != expected.job:
        raise OutputUncertain('輸出工作與收條不符，需核對。')


@dataclass(frozen=True)
class OutputReceipt:
    id: str
    owner: str
    state: OutputState
    name: str
    sha256: str
    source: str
    kind: str = 'calendar_csv_pending_reminder'
    job: str | None = None
    size: int = 0
    path: str | None = None
    revision: int = 0
    projection_pending: bool = False
    review_required: bool = False


@dataclass(frozen=True)
class ReviewClaim:
    record: OutputReceipt
    token: str


class OutputReceipts(Protocol):
    def get(self, key: str, owner: str) -> OutputReceipt | None: ...
    def begin(self, key, owner, name, digest, source, *, kind='calendar_csv_pending_reminder', job=None, size=0): ...
    def mark(self, record: OutputReceipt, state: OutputState) -> OutputReceipt | None: ...
    def request_delete(self, record: OutputReceipt, path: str) -> OutputReceipt | None: ...
    def finish_delete(self, record: OutputReceipt) -> OutputReceipt | None: ...
    def claim_reviews(self, limit=32) -> list[ReviewClaim]: ...
    def renew_review(self, claim: ReviewClaim) -> bool: ...
    def resolve_review(self, claim: ReviewClaim, state: OutputState, *, verification=None) -> ReviewClaim | None: ...
    def finish_review(self, claim: ReviewClaim, *, published=False): ...


class MemoryOutputReceipts:
    """Test adapter with the same stale-snapshot and lease checks as PostgreSQL."""
    def __init__(self):
        self._records, self._lock, self._reviews = {}, RLock(), {}

    def get(self, key, owner):
        with self._lock:
            record = self._records.get(key)
            return record if record and record.owner == owner else None

    def _save(self, record, **values):
        result = replace(record, revision=record.revision + 1, **values)
        self._records[result.id] = result
        self._reviews.setdefault(result.id, {'at': time.time()+30, 'until': 0, 'token': None, 'failures': 0})
        return result

    def begin(self, key, owner, name, digest, source, *, kind='calendar_csv_pending_reminder', job=None, size=0):
        with self._lock:
            old = self._records.get(key)
            if old is not None and old.owner != owner:
                raise ValueError('Output receipt ownership mismatch.')
            if old is None or old.state == 'not_written':
                record = OutputReceipt(key, owner, 'writing', name, digest, source, kind, job, size,
                                       revision=old.revision if old else 0)
                self._reviews[key] = {'at': time.time()+30, 'until': 0, 'token': None, 'failures': 0}
                return self._save(record), True
            return old, False

    def _current(self, record):
        current = self.get(record.id, record.owner)
        if current is not None:
            require_same_output(current, record)
        return current

    def mark(self, record, state):
        if state not in {'stored', 'unknown', 'not_written'}:
            raise ValueError('Invalid output transition.')
        with self._lock:
            current = self._current(record)
            if current is None or current.revision != record.revision or current.state not in WRITE_PENDING:
                return current
            result = self._save(current, state=state, projection_pending=bool(current.job and state == 'stored'),
                                review_required=False if state == 'stored' else current.review_required)
            self._reviews[result.id]['at'] = 0 if result.projection_pending else time.time()+30
            return result

    def request_delete(self, record, path):
        with self._lock:
            current = self._current(record)
            if current is None or current.revision != record.revision or current.state in {'not_written','deleted'}:
                return None
            result = self._save(current, state='deleting', path=path, projection_pending=bool(current.job), review_required=False)
            self._reviews[result.id]['at'] = time.time()+30
            return result

    def finish_delete(self, record):
        with self._lock:
            current = self._current(record)
            if current is None or current.revision != record.revision or current.state not in DELETE_PENDING:
                return current
            result = self._save(current, state='deleted', projection_pending=bool(current.job))
            self._reviews[result.id]['at'] = 0
            return result

    def for_jobs(self, owner, jobs):
        with self._lock:
            return [r for r in self._records.values() if r.owner == owner and r.job in jobs
                    and r.state in {'stored','deleting','delete_unknown','deleted'}]

    def claim_reviews(self, limit=32):
        with self._lock:
            claims, now = [], time.time()
            for key, review in sorted(self._reviews.items(), key=lambda pair: (pair[1]['at'],pair[0])):
                record = self._records[key]
                if ((record.review_required or record.state not in WRITE_PENDING | DELETE_PENDING) and not record.projection_pending
                        or review['at'] > now or review['until'] > now):
                    continue
                token = str(uuid.uuid4())
                review.update(token=token, until=now+60)
                claims.append(ReviewClaim(record, token))
                if len(claims) >= limit:
                    break
            return claims

    def _claimed(self, claim):
        current = self.get(claim.record.id, claim.record.owner)
        review = self._reviews.get(claim.record.id, {})
        return (current is not None and current.revision == claim.record.revision
                and review.get('token') == claim.token and review.get('until',0) > time.time())

    def renew_review(self, claim):
        with self._lock:
            if not self._claimed(claim):
                return False
            self._reviews[claim.record.id]['until'] = time.time()+60
            return True

    def resolve_review(self, claim, state, *, verification=None):
        validate_review_transition(claim.record.state, state)
        with self._lock:
            if not self._claimed(claim):
                return None
            review = self._reviews[claim.record.id]
            mismatch = verification is not None and verification.outcome == 'mismatch'
            review['mismatches'] = review.get('mismatches', 0)+1 if mismatch else 0
            if verification is not None:
                review.update(outcome=verification.outcome, reason=verification.reason)
            record = self._save(claim.record, state=state,
                                review_required=review['mismatches'] >= 3,
                                projection_pending=claim.record.projection_pending or bool(claim.record.job and state in {'stored','deleted'}))
            return ReviewClaim(record, claim.token)

    def finish_review(self, claim, *, published=False):
        with self._lock:
            if not self._claimed(claim):
                return False
            record = self._records[claim.record.id]
            pending = record.projection_pending and not published
            self._records[record.id] = replace(record, projection_pending=pending)
            review = self._reviews[record.id]
            retry = (not record.review_required and record.state in WRITE_PENDING | DELETE_PENDING) or pending
            review['failures'] = review['failures']+1 if retry else 0
            review.update(at=time.time()+min(300,2**min(review['failures'],9)) if retry else 0,
                          until=0, token=None)
            return True


def validate_review_transition(before, after):
    if not (before in WRITE_PENDING and after in {'stored','unknown'}
            or before in DELETE_PENDING and after in {'deleted','delete_unknown'}):
        raise ValueError('Invalid read-only review transition.')
