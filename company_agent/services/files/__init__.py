"""services files package; no service initialization on import."""
from __future__ import annotations


def block_page(blocks, start, limit):
    """Project one complete source page without changing the stored blocks."""
    end = min(len(blocks), start + limit)
    return {
        'blocks': blocks[start:end],
        'total_blocks': len(blocks),
        'next_start': end if end < len(blocks) else None,
    }
