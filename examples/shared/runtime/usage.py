"""Task-local usage attribution; document service traffic keeps its own budget."""
from __future__ import annotations

from contextvars import ContextVar

from examples.identity.providers.subjects import linked_subject

document_subject: ContextVar[str | None] = ContextVar('official_document_usage_subject', default=None)


def request_metadata() -> dict:
    """Only a server-verified subject may be placed in persisted accounting."""
    values = {'ai_usage_source': 'official_document'}
    subject = document_subject.get()
    if subject:
        values['ai_usage_subject'] = subject
    # Custom keys outside this LiteLLM container are not retained in spend logs.
    return {'spend_logs_metadata': values}


def verified_subject(user: dict) -> str:
    """Validate the server identity projection, never a PDF or browser field."""
    subject = linked_subject(user.get('oauth'))
    if user.get('role') not in {'user', 'admin'} or (
        subject is None or subject != subject.strip() or len(subject) > 255
    ):
        raise ValueError('公文用量缺少已驗證的使用者身分，請重新登入。')
    return subject
