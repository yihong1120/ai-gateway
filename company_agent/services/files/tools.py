"""LangChain tool adapters for file, Office and CAD business operations."""
from __future__ import annotations

import asyncio
from pathlib import PurePosixPath

from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from company_agent.runtime.tools import injected_tool
from company_agent.runtime.tools import tool_receipt
from company_agent.services.cad.schemas import CreateCad
from company_agent.services.cad.schemas import EditCad
from company_agent.services.cad.schemas import ReadCad
from company_agent.services.files.operations import ToolBox
from company_agent.services.files.schemas import Publish
from company_agent.services.files.schemas import Read
from company_agent.services.files.schemas import ReadOCR
from company_agent.services.files.schemas import Search
from company_agent.services.office.schemas import AnalyzeSpreadsheet
from company_agent.services.office.schemas import Edit
from company_agent.services.office.schemas import EditSpreadsheet
from company_agent.services.office.schemas import ReadSpreadsheet
from company_agent.services.office.schemas import Slides
from company_agent.services.office.schemas import Spreadsheet
from company_agent.services.office.schemas import SpreadsheetReport
from company_agent.services.office.schemas import Text
from company_agent.services.office.schemas import Word
from official_document_workflow.infrastructure.concurrency import run_ocr


FILE_TOOL_SPECS = {
    'edit_spreadsheet': (EditSpreadsheet, '指定來源 XLSX 的 SHA256、工作表／儲存格、expected 原值（公式填含 = 原式）、新值；可跨表一次修改最多 100 格，formula=true 才建立受限同表公式，style_from 複製同表既有儲存格樣式。先 read_spreadsheet，另存不覆寫。保留其他封裝部件；公式快取清除等待 Excel 重算，不插刪列、不處理巨集／外連／受保護／陣列公式。'),
    'read_ocr': (ReadOCR, '本機 PaddleOCR 逐頁辨識 PDF／PNG／JPEG，分頁回傳版面文字及位置、來源 SHA256。同頁本工作快取；不作完整視覺或工程尺寸判讀。不重試失敗 OCR。'),
    'create_spreadsheet_report': (SpreadsheetReport, '新建 XLSX 計算報表：明確資料列、另外指定公式及 bar/line 圖表。隔離 LibreOffice 計算並校驗；公式僅同表參照及 + - * /、SUM/AVERAGE/MIN/MAX/COUNT/ROUND/ABS，目標須為資料列中 null 空格。不是來源原簿修改。'),
    'read_cad': (ReadCad, '在隔離環境分頁讀取 DWG／DXF 結構、文字、圖層、圖元及 SHA256。limit 最大 10，建議省略使用預設值；start 使用 next_start 續頁，不能擴大 limit。非視覺判圖，未知物件／外參不完整；先 summary 再依 view/query 取所需內容。'),
    'create_cad': (CreateCad, '產生全新、可編輯 2D DXF 草稿（線／圓／文字），必須指定 mm 或 m。不是 DWG 轉檔，不重建原圖，不寫 NAS。'),
    'edit_cad_text': (EditCad, '只對簡單 R2018 ASCII DXF 的 TEXT 圖元做 SHA256／handle／原文精確修改並另存草稿。須先 read_cad；拒絕 DWG、外參、複雜圖塊及附加資料；不是整圖無損編輯。'),
    'search_files': (Search, '在授權根目錄搜尋檔名或全文字面片段；搜尋不是語意完整索引，有掃描上限。'),
    'read_file': (Read, '依格式唯讀解析來源：TXT/MD/CSV/PDF/DOCX/PPTX 回傳文字區塊、run 位置及 SHA256。XLSX 直接使用結構化 Excel 解析器，回傳第一工作表 rows/cells、sheets 清單、公式及未驗證快取；start 為零起算列，limit 為列數，只讀前20欄。指定工作表或更多欄用 read_spreadsheet，統計用 analyze_spreadsheet。私人草稿用 inspect_artifact。'),
    'create_word': (Word, '建立新的可編輯 DOCX 草稿；不改原檔，不寫 NAS。'),
    'create_slides': (Slides, '建立 PPTX：最外層只有 name 與 slides，slides 每頁為 {title,bullets}；不要新增最外層 title。保守限制每頁文字，交付前仍需視覺校對。'),
    'edit_text_run': (Edit, '使用 read_file 回傳的精確 run 位置和版本修改 DOCX/PPTX，另存草稿；不重建其他內容。'),
    'create_text': (Text, '建立 UTF-8 txt/md/csv 草稿；不寫 NAS。'),
    'propose_publish': (Publish, '提出將本次草稿存到授權位置的申請，必須本人在模型之外確認才寫入，禁止覆寫。'),
    'read_spreadsheet': (ReadSpreadsheet, '分頁讀取 XLSX 儲存格及位置、工作表清單和來源版本；公式與快取值分開標示，不計算公式。'),
    'analyze_spreadsheet': (AnalyzeSpreadsheet, '對 XLSX 指定工作表的一欄和列範圍計算數值 count/sum/min/max/mean；排除公式、文字、日期和布林，不執行程式。'),
    'create_spreadsheet': (Spreadsheet, '建立可編輯 XLSX 草稿，使用具型別的列和儲存格；所有字串都是文字，不建立公式，最多 20 表、每表 1000 列/100 欄、總 50000 格。不寫 NAS。'),
}

MUTATING_TOOLS = {
    'create_word', 'create_slides', 'create_text', 'edit_text_run',
    'create_spreadsheet', 'create_cad', 'edit_cad_text',
    'create_spreadsheet_report', 'edit_spreadsheet',
}
SOURCE_TOOLS = {
    'read_file', 'read_spreadsheet', 'analyze_spreadsheet', 'read_cad', 'read_ocr',
}


async def file_tool(runtime: ToolRuntime, *, tool_name, input_schema, **kwargs):
    name, schema = tool_name, input_schema
    context = runtime.context
    box = ToolBox(context.settings, context.store, context.owner, context.job)
    box.sources = {
        (s['root'], s['path']): s for s in runtime.state.get('sources', [])
    }
    args = schema.model_validate(kwargs)
    plain = args.model_dump()
    if 'root' in plain:
        await asyncio.to_thread(
            box.access.allowed, plain['root'], write=name == 'propose_publish',
        )
    if name == 'read_file' and PurePosixPath(plain['path']).suffix.lower() in {
        '.dwg',
        '.dxf',
    }:
        # Routing information, not a failed parser or a source receipt.
        # No file bytes are fetched and no alternative reader is run.
        result = {
            'status': 'needs_specialist',
            'executed': False,
            'agent': 'engineering',
            'tool': 'read_cad',
            'root': plain['root'],
            'path': plain['path'],
            'notice': '這是 CAD 檔案，尚未讀取內容。請交回協調者委派 engineering 使用 read_cad；勿再用 read_file。',
        }
        await asyncio.to_thread(
            context.store.audit, context.owner, context.job, 'cad_format_route', 'not_executed',
        )
        return Command(
            update={
                'messages': [tool_receipt(runtime.tool_call_id, name, result)],
                'accessed_roots': [plain['root']],
            },
        )
    runtime.stream_writer({'stage': 'tool', 'tool': name})
    mutating = name in MUTATING_TOOLS
    result = (
        await asyncio.to_thread(
            context.store.begin_tool, context.owner, context.job, runtime.tool_call_id, name, plain,
        )
        if mutating
        else None
    )
    if result is None:
        try:
            result = await (run_ocr if name == 'read_ocr' else asyncio.to_thread)(getattr(box, name), args)
        except FileNotFoundError as failure:
            # Only route a missing, exact single-file name known in this job.
            # ACL/path validation ran first. Existing same-named sources still
            # use the normal reader; unknown paths and parser failures propagate.
            matches = [
                file for file in await asyncio.to_thread(context.store.files, context.owner, context.job)
                if file['name'] == plain.get('path')
            ] if name in {'read_file', 'read_spreadsheet'} else []
            if not matches or failure.filename != plain['path']:
                raise
            return Command(
                update={
                    'messages': [
                        tool_receipt(
                            runtime.tool_call_id, name, {
                                'status': 'needs_artifact_reader', 'executed': False, 'tool': 'inspect_artifact',
                                'artifacts': matches,
                                'notice': '此名稱是本工作資料庫草稿，未存入來源根目錄，尚未讀取。請使用真實 artifact_id 呼叫 inspect_artifact；若本角色無此工具，交回協調者委派 document 或 reviewer。不可把草稿當來源路徑。',
                            },
                        ),
                    ],
                },
            )
        if mutating:
            await asyncio.to_thread(
                context.store.finish_tool, context.owner, context.job, runtime.tool_call_id, result,
            )
    await asyncio.to_thread(context.store.audit, context.owner, context.job, name, 'done')
    update = {
        'messages': [tool_receipt(runtime.tool_call_id, name, result)],
        'sources': (
            [box.sources[(plain['root'], plain['path'])]]
            if name in SOURCE_TOOLS
            else []
        ),
    }
    if 'root' in plain:
        update['accessed_roots'] = [plain['root']]
    if name == 'propose_publish' and result['status'] == 'pending':
        update['pending'] = {
            'proposal_id': result['proposal_id'],
            'tool_call_id': runtime.tool_call_id,
        }
    return Command(update=update)


def file_tools():
    return [
        injected_tool(name, schema, description, file_tool, tool_name=name, input_schema=schema)
        for name, (schema, description) in FILE_TOOL_SPECS.items()
    ]
