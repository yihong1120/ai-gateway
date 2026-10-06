"""Display provenance only; these timestamps never feed calendar extraction."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .netask import DocumentRecord


def document_recorded_at(document: DocumentRecord) -> str | None:
    """EIP folder date is NOT verified personal receipt or document deadline."""
    value = document.get('date')
    if value in (None, False):
        return None
    # Reject date-only values: do not fabricate a midnight arrival time.
    if len(value) < 19 or value[10] not in (' ', 'T'):
        return None
    stamp = datetime.fromisoformat(value)
    zone = ZoneInfo('Asia/Taipei')
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=zone)
    return stamp.astimezone(zone).isoformat(timespec='seconds')
