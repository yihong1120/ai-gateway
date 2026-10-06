"""Read only the owner's CSV identity and metadata needed for replay checks."""
from __future__ import annotations

import json

from open_webui.internal.db import get_async_db_context
from open_webui.models.files import File
from sqlalchemy import select


async def prior_csv_ids(owner, current_id, source_ids):
    source_ids = frozenset(json.dumps(value, sort_keys=True) for value in source_ids)
    async with get_async_db_context() as db:
        result = await db.execute(
            select(File.id, File.meta).where(
                File.user_id == owner, File.id != current_id, File.filename.ilike('%.csv'),
            ),
        )
        candidates = result.all()
    return [
        file_id for file_id, meta in candidates
        if meta and len(meta.get('eip_group_source_ids', ())) == len(source_ids)
        and frozenset(
            json.dumps(value, sort_keys=True)
            for value in meta.get('eip_group_source_ids', ())
        ) == source_ids
    ]
