"""Read completed receipts through the repository owned by this application."""
from __future__ import annotations


class OutputCatalog:
    def __init__(self, repository):
        self.repository = repository

    def files_for_jobs(self, owner, jobs):
        grouped = {}
        for receipt in self.repository.for_jobs(owner, jobs):
            grouped.setdefault(receipt.job, []).append(
                {
                    'id': receipt.id,
                    'name': receipt.name,
                    'state': receipt.state,
                    'kind': 'document_pdf'
                    if receipt.kind == 'renamed_pdf'
                    else receipt.kind,
                    'url': f'/api/v1/files/{receipt.id}/content?attachment=true',
                },
            )
        return grouped
