"""Pure, shared identity rules and bounded content proof for every file adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Literal

from examples.official_document_workflow.domain.documents import OutputUncertain


class OutputMismatch(OutputUncertain):
    def __init__(self, reason, proof=None):
        self.reason, self.proof = reason, proof or {}
        super().__init__('輸出內容或來源與收條不符，請人工核對。')


@dataclass(frozen=True)
class FileVerification:
    outcome: Literal['verified', 'not_found', 'mismatch', 'unavailable']
    reason: str = ''
    proof: dict = field(default_factory=dict)
    file: object | None = field(default=None, repr=False, compare=False)


def verify_metadata(receipt, item):
    if not isinstance(item, dict) or not isinstance(item.get('meta'), dict):
        raise OutputMismatch('invalid_file_metadata')
    meta = item['meta']
    origin = meta.get('data') or {}
    if not isinstance(origin, dict):
        raise OutputMismatch('invalid_file_metadata')
    if (
        item.get('id') != receipt.id
        or item.get('user_id') != receipt.owner
        or meta.get('file_hash') != receipt.sha256
        or origin.get('source_file_id') != receipt.source
        or origin.get('kind') != receipt.kind
        or origin.get('generated_by') != 'official_document_processor_pipe'
        or receipt.name
        and meta.get('name', item.get('filename', '')) != receipt.name
    ):
        raise OutputMismatch('identity_or_provenance_mismatch')


class ContentProof:
    def __init__(self, receipt):
        self.receipt = receipt
        self.digest, self.size = hashlib.sha256(), 0
        self.limit = receipt.size or 20_000_000

    def update(self, block):
        self.size += len(block)
        if self.size > self.limit:
            raise OutputMismatch(
                'file_size_limit', {'bytes': self.size, 'limit': self.limit}
            )
        self.digest.update(block)

    def finish(self):
        proof = {'sha256': self.digest.hexdigest(), 'bytes': self.size}
        if (
            proof['sha256'] != self.receipt.sha256
            or self.receipt.size
            and self.size != self.receipt.size
        ):
            raise OutputMismatch('content_mismatch', proof)
        return proof


@dataclass(frozen=True)
class DeletionVerification:
    outcome: Literal['deleted', 'residual', 'unavailable', 'mismatch']
    reason: str = ''
    proof: dict = field(default_factory=dict)
