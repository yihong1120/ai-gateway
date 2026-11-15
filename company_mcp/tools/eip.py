"""EIP read contracts share the existing business validators."""
from company_agent.services.eip.schemas import CalendarQuery, EIPAttachments, EIPQuery, EIPRead
from .common import Workspace


class Documents(Workspace, EIPQuery):
    pass


class Document(Workspace, EIPAttachments):
    pass


class Attachment(Workspace, EIPRead):
    pass


class Calendar(Workspace, CalendarQuery):
    pass


TOOLS = {
    'eip_query': (Documents, '分頁查本人 EIP 公文匣；不是行事曆，沒有簽核操作。'),
    'eip_document_read': (Document, '讀本人公文內文及附件清單，不呼叫 LLM，不簽核。'),
    'eip_attachments': (Document, '取得公文附件 handle；上游可能更新閱讀紀錄，不簽核。'),
    'eip_read_attachment': (Attachment, '分頁讀取 PDF／DOCX／DOC 附件文字；可明確要求本地 OCR，不付費分析。'),
    'eip_calendar_query': (Calendar, '讀本人可見個人與分享行程，最多 93 天；不新增或刪除。'),
    'eip_calendar_detail': (Document, '讀指定行程詳細內容及附件 handle；唯讀。'),
}
