"""Only generated reports may be recalculated in the shared offline sandbox."""
from __future__ import annotations

from company_agent.services.sandbox import sandbox_worker

IMAGE = 'ai-gateway/company-office:1'


def office_worker(operation, data=None, suffix='.pdf', **args):
    if operation not in {'report', 'ocr_page'} or suffix not in {
        '.pdf',
        '.png',
        '.jpg',
        '.jpeg',
    }:
        raise ValueError('不支援的 Office 操作。')
    if operation == 'ocr_page' and (not data or len(data) > 20000000):
        raise ValueError('來源大小不正確。')
    return sandbox_worker(
        IMAGE, {'operation': operation, 'suffix': suffix, **args}, data=data, suffix=suffix,
        output='page.png' if operation == 'ocr_page' else 'report.xlsx', tmpfs_mb=256,
    )
