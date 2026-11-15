"""Thin official tool adapters over business services."""
from __future__ import annotations

from functools import partial

from langgraph.prebuilt import ToolRuntime

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.eip import client
from company_agent.services.eip.schemas import CalendarPrepare
from company_agent.services.eip.schemas import CalendarQuery
from company_agent.services.eip.schemas import EIPAttachments
from company_agent.services.eip.schemas import EIPProcess
from company_agent.services.eip.schemas import EIPQuery
from company_agent.services.eip.schemas import EIPRead


async def emit_progress(event, *, runtime, name):
    if event.get('type') == 'status':
        runtime.stream_writer(
            {
                'stage': 'tool',
                'tool': name,
                'detail': str(event.get('data', {}).get('description', ''))[:800],
            },
        )


async def eip_tool(runtime: ToolRuntime, *, tool_name, input_schema, **kwargs):
    name, schema = tool_name, input_schema
    runtime.stream_writer({'stage': 'tool', 'tool': name})
    args = schema.model_validate(kwargs)
    if name == 'eip_read_attachment':
        from company_agent.services.eip.reading import read_attachment

        result = await read_attachment(runtime.context, args)
    elif name == 'eip_process_attachments':
        from company_agent.services.eip.processing import process

        result = await process(runtime.context, args, emitter=partial(emit_progress, runtime=runtime, name=name))
    elif name == 'eip_calendar_prepare':
        from company_agent.services.eip.drafts import prepare

        result = await prepare(runtime.context, args)
    else:
        result = await client.query(runtime.context, name, args, runtime.tool_call_id)
    runtime.context.store.audit(
        runtime.context.owner, runtime.context.job, name, 'done',
    )
    runtime.stream_writer({'stage': 'tool', 'tool': name})
    return tool_receipt(runtime.tool_call_id, name, result)


def eip_tools():
    return [
        injected_tool(name, schema, description, eip_tool, tool_name=name, input_schema=schema)
        for name, schema, description in (
            (
                'eip_calendar_query',
                CalendarQuery,
                '查真正 EIP 行事曆清單，含本人可見個人與分享事項。以台北 YYYY-MM-DD 起訖日期查詢，最多 93 天。只含主旨與時間；需要詳細內容或附件時，再用該筆 handle 呼叫 eip_calendar_detail，不用公文文件匣代替；唯讀。',
            ),
            (
                'eip_calendar_detail',
                EIPAttachments,
                '讀取單筆 EIP 行程詳細頁：用本工作 eip_calendar_query 回傳的 handle，取得事項內容及附件名稱與閱讀 handle。詢問詳細內容、會勘事由、地點、出席者或附件時必須讀詳細頁，不能只看清單便說沒有內容。內容足夠時直接回報，需要附件正文才用 eip_read_attachment；唯讀，不修改行程。',
            ),
            (
                'eip_calendar_prepare',
                CalendarPrepare,
                '依使用者明確新增行程需求建立待確認行事曆 CSV。提供主旨、日期、全天或完整起訖時間；缺資料先詢問。尚未寫入，回傳後交 reviewer 檢查 CSV，並引導本人到 /agents 預覽確認才寫入私人行事曆；不分享、不寄信、不自動批准、不修改刪除既有行程。',
            ),
            (
                'eip_query',
                EIPQuery,
                '查本人 EIP 待處理 inbox、簽核紀錄 sign、通知 notify、送出 sent，明確日期及頁碼。每筆 view_link 可由本人直接開啟表單內文與附件；要求可點擊查看時查完即回傳連結，不另讀附件。這是公文清單，不是行事曆。需既有綁定；沒有簽核權。',
            ),
            (
                'eip_attachments',
                EIPAttachments,
                '用本工作 eip_query 的 handle 查附件清單及可處理 handle；可能更新已讀，不簽核。',
            ),
            (
                'eip_read_attachment',
                EIPRead,
                '只讀附件內容：用附件 handle 讀 PDF/DOCX/DOC 文字，start/next_start 分頁，每次最多 5 區塊。不產製 Word/PDF/CSV。mode=text 讀文字層，空白掃描頁可明確指定 mode=ocr,page 使用本地 OCR。只摘要／比對附件時優先此工具。',
            ),
            (
                'eip_process_attachments',
                EIPProcess,
                '下載本工作附件 handle（PDF/DOCX/DOC），同簽核合併分析，各自輸出 PDF／可編輯 Word，合併期限 CSV；使用本人模型額度。沒有期限不產行程。回傳私人下載檔；CSV 在工作台由本人預覽確認才匯入，工具不批准行事曆。不重試失敗處理。',
            ),
        )
    ]
