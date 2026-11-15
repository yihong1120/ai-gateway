"""User-facing progress never exposes raw model/tool payloads."""
from __future__ import annotations

ROLES = {
    'coordinator': '正在理解與安排工作',
    'eip': '正在查詢 EIP',
    'document': '正在處理文件',
    'data': '正在分析資料',
    'reviewer': '正在核對成果',
    'knowledge': '正在查找資料',
    'research': '正在查證公開資料',
    'engineering': '正在分析圖面',
}
TOOLS = {
    'eip_query': '正在查詢 EIP 文件清單',
    'eip_attachments': '正在取得 EIP 附件清單',
    'eip_read_attachment': '正在唯讀解析 EIP 附件內容',
    'eip_process_attachments': '正在處理 EIP 公文附件',
    'eip_calendar_query': '正在查詢 EIP 行事曆',
    'eip_calendar_detail': '正在讀取 EIP 行程詳細內容',
    'eip_calendar_prepare': '正在準備行程，待您確認後寫入',
}
STATUSES = {
    'done': '已完成',
    'needs_input': '等待您補充資料',
    'partial': '部分完成',
    'error': '執行未完成',
    'queued': '等待執行',
    'running': '正在執行',
    'interrupted': '執行已中斷',
    'cancelled': '已取消',
    'awaiting_approval': '等待您確認',
}


def progress_text(event):
    if isinstance(event, str):
        return event
    if not isinstance(event, dict):
        return '正在處理工作'
    if event.get('stage') == 'compact':
        return '正在壓縮較早對話，保留原始紀錄與近期訊息'
    if event.get('stage') == 'tool':
        return TOOLS.get(event.get('tool'), '正在執行工作步驟')
    return ROLES.get(event.get('agent'), '正在處理工作')
