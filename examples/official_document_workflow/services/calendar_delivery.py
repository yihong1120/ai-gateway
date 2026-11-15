"""Render reminder CSVs; reservation and verification belong to OutputDelivery."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from typing import Literal

from examples.official_document_workflow.application.output_receipts import require_same_output
from examples.official_document_workflow.domain.documents import OutputNotWritten
from examples.shared.runtime.io import run_sync


@dataclass(frozen=True)
class CalendarDelivery:
    state: Literal['stored', 'not_written', 'unknown']
    name: str = ''
    file: object | None = None
    output_id: str | None = None


class CalendarOutputDelivery:
    def __init__(self, receipts, exports, values):
        self.receipts, self.exports, self.values = receipts, exports, values

    @staticmethod
    def key(record):
        identity = json.dumps(
            [
                record.scope.user_id, record.scope.chat_id, record.created_at,
                record.attachment_fingerprint,
            ], separators=(',', ':'),
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    @staticmethod
    def name(payload):
        return (Path(payload.csv_base_name).stem or '行事曆') + '_補充提醒.csv'

    async def recover(self, record):
        key = self.key(record)
        try:
            receipt = await run_sync(self.receipts.get, key, record.scope.user_id)
            if receipt is None or not record.payload.source_file_ids:
                return CalendarDelivery('unknown', output_id=key)
            # A continuation has no new output body. Its receipt remains the
            # content expectation; owner/source/name/type must still match the
            # original continuation, and the shared finder verifies real bytes.
            expected = replace(
                receipt, id=key, owner=record.scope.user_id, name=self.name(record.payload),
                source=record.payload.source_file_ids[0], kind='calendar_csv_pending_reminder',
            )
            require_same_output(receipt, expected)
            if receipt.state == 'not_written':
                return CalendarDelivery('not_written', receipt.name, output_id=key)
            item = await self.exports.delivery.recover(receipt, expected=expected)
            return CalendarDelivery('stored', receipt.name, item, key)
        except Exception:
            return CalendarDelivery('unknown', output_id=key)

    async def deliver(self, record, payload, rows):
        key = self.key(record)
        try:
            if not payload.source_file_ids:
                raise OutputNotWritten('來源附件已失效。')
            data, name = self.values.calendar_csv(list(rows)), self.name(payload)
        except Exception:
            return CalendarDelivery('not_written', output_id=key)
        try:
            item = await self.exports.store_output(
                user_id=record.scope.user_id, payload=data, display_name=name,
                content_type='text/csv; charset=utf-8', source_file_id=payload.source_file_ids[0],
                kind='calendar_csv_pending_reminder', output_id=key,
                extra_metadata={
                    'official_document_pending_reminder': {
                        'source_file_ids': list(payload.source_file_ids), 'batch_mode': payload.batch_mode, 'output_id': key,
                    },
                },
            )
            if item.id != key:
                return CalendarDelivery('unknown', output_id=key)
            return CalendarDelivery('stored', name, item, key)
        except OutputNotWritten:
            return CalendarDelivery('not_written', name, output_id=key)
        except Exception:
            return CalendarDelivery('unknown', name, output_id=key)
