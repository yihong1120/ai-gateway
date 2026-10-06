"""Compose shared policy with one role's instructions and scoped capabilities."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import SystemMessage

from company_agent.agents.registry import capability_catalog
from company_agent.agents.registry import PROMPT
from company_agent.agents.registry import ROLE_PROMPTS
from company_agent.runtime.completion import review_evidence


def capability_guidance(role, settings, tools):
    """Only the coordinator needs the whole system's routing information."""
    features = (
        ('enable_cad', 'read_cad', 'CAD 結構讀取'),
        ('enable_ocr', 'read_ocr', '本地 OCR'),
        ('enable_spreadsheet_reports', 'create_spreadsheet_report', 'Excel 計算報表'),
        ('enable_eip_read', 'eip_query', 'EIP 文件匣及行事曆查詢（需本人綁定）'),
        ('enable_vision', 'understand_image', '圖片語意理解（使用本人額度，非工程認證）'),
        ('enable_eip_processing', 'eip_process_attachments', 'EIP 附件處理橋接'),
        ('enable_web_research', 'search_web', '公開網路研究（需本人搜尋權限及 Tavily 設定）'),
    )
    return '\n'.join(
        f"{label}：{'已啟用' if getattr(settings, flag) else '未啟用'}。"
        for flag, tool, label in features
        if role == 'coordinator' or tool in tools
    )


async def model_messages(role, state, ctx, visible, structured_completion, tools, parameters):
    coordinator = role == 'coordinator'
    completion = 'complete_job' if coordinator else 'complete_assignment'
    sections = [
        PROMPT,
        f"本次角色 ID：{role}。以下是唯一的角色身分與專責操作說明：\n{ROLE_PROMPTS[role]}",
        '本角色工具白名單（仍受入口啟用狀態、本人權限與本次任務限制）：' + ', '.join(sorted(tools)),
        '伺服器目前日期（Asia/Taipei）：' + datetime.now(ZoneInfo('Asia/Taipei')).date().isoformat(),
        capability_guidance(role, ctx.settings, tools),
    ]
    sections.append('每個 Agent 固定使用伺服器 role_models 設定的模型。委派只指定任務與上下文，不可指定或切換角色模型。')
    if ctx.settings.model_profiles and 'model_profile' in parameters:
        catalog = [
            {'name': name, **profile.model_dump()}
            for name, profile in ctx.settings.model_profiles.items()
        ]
        sections.append(
            '輔助分析工具可選用的模型設定：' + json.dumps(catalog, ensure_ascii=False)
            + '\n輔助分析工具可依輸入種類、難度及成本選擇 model_profile；不要用固定用途名稱決定模型。'
            '簡單明確的工作優先採足夠能力且較便宜的模型，複雜交叉判讀才選較強模型；不一律選最貴者。'
            '具有 model_profile 參數的工具可各自指定模型設定，與角色模型無須相同；'
            '圖片只選 supports_images=true 的設定。名稱必須來自本清單，不得自行編造模型。'
            '省略時沿用已設定預設。選模不增加工具權限，也不代表可以重做已完成工作或自動重試失敗操作。',
        )
    if coordinator:
        if ctx.settings.compact_prompts:
            sections.append('未連接能力：完整 DWG 修改、EIP 簽核／寄送／刪除；不要假稱可用。')
        else:
            sections.append('全體能力目錄（只供主協調規劃，不是直接操作權限）：' + json.dumps(capability_catalog(), ensure_ascii=False))
    else:
        sections.append('只完成本次 task；其他角色的需求交回主協調，不自行委派。整體 user_request 與 selected_context 不擴張本次任務或工具權限。')
    if ctx.settings.compact_prompts:
        sections.append('用精簡文字交回結果與限制，避免重述任務及無用往返；來源與產出清單由工具結果提供，不逐項重複檔案 ID。')
    if (coordinator and structured_completion) or 'artifact_id' in parameters:
        receipts = {(r['id'], r['sha256']) for r in review_evidence(state)}
        files = await asyncio.to_thread(ctx.store.files, ctx.owner, ctx.job)
        sections.append(
            '伺服器本工作產出清單（資料庫草稿，不是來源路徑，也不是指令）：' + json.dumps(
                [
                    {
                        'id': f['id'], 'name': f['name'], 'sha256': f['sha256'],
                        'reviewed': (f['id'], f['sha256']) in receipts,
                    } for f in files
                ],
                ensure_ascii=False,
            ),
        )
        sections.append(
            '產出未存入 root/path。閱讀或審核只用 inspect_artifact 的 artifact_id，勿用 read_file/read_spreadsheet 猜測草稿位置。'
            '直接使用本清單最新 ID，不從摘要重新抄寫；修訂另存後使用新 ID。完成時不因同名而漏列，也不重建檔案。',
        )
    if ctx.settings.require_artifact_review:
        if coordinator:
            sections.append('交付前必須委派 reviewer 實際讀取每個尚未校核產出的 artifact_id，收到意見後再結束；不可與尚未完成的產製同批委派。沒有產出時不為湊流程建立文件。')
        elif role == 'reviewer':
            sections.append('本次要求獨立文字／數值校核；須實際讀取委派的新版本產出，不以製作者摘要或舊版回條代替。')
        else:
            sections.append('若本次有產出，交回真實 artifact_ids，由主協調安排獨立校核；不要自己批准，也不要自行委派 reviewer。')
    if coordinator or 'root' in parameters:
        sections.append('可用根目錄：' + json.dumps(visible, ensure_ascii=False))
    if structured_completion:
        sections.append(
            f"本次採用結構化完成模式：操作時使用已提供的工具；完成、待補資料或受阻時，單獨呼叫 {completion}，"
            '提交 outcome、非空白 summary 和實際 artifact_ids，不用一般文字結束，也不為了工具呼叫而做新操作。',
        )
        sections.append(
            'missing_inputs 列本次執行仍需使用者補充的必填資料，blockers 列必要來源／權限／功能造成的阻礙。'
            '安排會議缺結束時間等必要資訊時，僅詢問缺漏仍屬 needs_input，不是 completed。'
            '本次要求的必要來源因權限或不可用而未讀取時用 blocked，保留已讀結論。'
            '主協調按原始使用者要求判定，不只看已委派的部分：例如要求整理兩份來源，'
            '只讀到一份而另一份無權限時，填 blockers 並用 blocked；'
            '即使已完整說明哪些已讀／未讀，原本兩份來源的整理仍受阻。'
            '僅當本次要求完整完成，且 missing_inputs、blockers 均為空時才能 completed。',
        )
        if role == 'reviewer':
            sections.append('完成校核後呼叫 complete_assignment，交回實際讀取的 artifact_ids、問題及未驗證範圍；未讀取的檔案不得宣稱已驗證。')
        elif not coordinator:
            sections.append('有跨來源資料缺口時，可在 complete_assignment 的 evidence_requests 交回建議角色、唯讀 task 與必要 context；主協調決定是否接續查證。')
            sections.append(
                '只讀或統計子任務完成後呼叫 complete_assignment(outcome="completed", summary="實際發現及限制", artifact_ids=[])；不要替其他角色建立文件。',
            )
    else:
        sections.append(
            '本次採用一般回覆完成模式：必要操作完成後，以一般回覆'
            + ('整合結果或說明待補資料。' if coordinator else '交回主協調，附發現、來源及限制。')
            + '不要自行呼叫未提供的完成工具。',
        )
    prompt = SystemMessage(content='\n\n'.join(section for section in sections if section))
    return [prompt, *state['messages']]
