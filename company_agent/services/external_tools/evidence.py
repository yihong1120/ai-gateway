"""Transport provenance without interpreting any service's domain response."""
from __future__ import annotations

import json
from datetime import datetime


def read_results(receipts):
    results = []
    for receipt in receipts:
        queried_at = datetime.fromisoformat(receipt['queried_at'])
        if queried_at.utcoffset() is None:
            raise ValueError('外部工具查詢時間缺少時區。')
        result = {
            'kind': 'external_tool',
            'source': receipt['source'],
            'server': receipt['server'],
            'tool': receipt['tool'],
            'queried_at': receipt['queried_at'],
        }
        # The source service owns public provenance (e.g. query/page coverage).
        # Forward it as bounded data; the gateway has no domain schemas or IDs.
        payload = receipt.get('result')
        if isinstance(payload, dict) and isinstance(payload.get('provenance'), dict):
            metadata = payload['provenance']
            if len(json.dumps(metadata, ensure_ascii=False).encode()) > 8192:
                result['provenance_truncated'] = True
            else:
                result['provenance'] = metadata
        results.append(result)
    return results
