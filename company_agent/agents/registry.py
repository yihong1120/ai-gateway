"""Static, explicit registration. Adding a role does not patch the graph loop."""
from __future__ import annotations

from importlib.resources import files
from types import MappingProxyType

from company_agent.agents.coordinator import SPEC as coordinator
from company_agent.agents.data import SPEC as data
from company_agent.agents.document import SPEC as document
from company_agent.agents.eip import SPEC as eip
from company_agent.agents.engineering import SPEC as engineering
from company_agent.agents.knowledge import SPEC as knowledge
from company_agent.agents.research import SPEC as research
from company_agent.agents.reviewer import SPEC as reviewer

SPECS = MappingProxyType(
    {
        spec.name: spec
        for spec in (coordinator, eip, engineering, research, knowledge, document, data, reviewer)
    },
)
ROLE_TOOLS = MappingProxyType({name: spec.tools for name, spec in SPECS.items()})
ROLE_PROMPTS = MappingProxyType({name: spec.prompt() for name, spec in SPECS.items()})
PROMPT = files('company_agent.agents').joinpath('common.md').read_text(encoding='utf-8')


def capability_catalog():
    return {
        'coordinator': {'status': 'implemented', 'scope': '理解指令、委派、整合；不直接操作外部系統'},
        'knowledge': {'status': 'implemented', 'scope': '授權本機及個人綁定 NAS home／共享資料夾的搜尋與文字閱讀；NAS 唯讀'},
        'document': {'status': 'implemented', 'scope': 'DOCX／PPTX／文字草稿、來源局部修改、受控存檔；本工作草稿可用 edit_artifact_text_run 依已讀 run／雜湊另存修訂版並重新審核；本地 OCR 讀取文字；啟用 understand_image 可對 PNG/JPEG/PDF 單頁語意分析，非工程認證'},
        'data': {'status': 'implemented', 'scope': 'XLSX 分頁讀取、單欄統計、新建公式與圖表報表；edit_spreadsheet 指定原簿 SHA／原值，跨表修改值、受限同表公式及既有樣式後另存；清除快取待 Excel 重算，不插刪列或執行巨集'},
        'reviewer': {'status': 'implemented', 'scope': '只讀來源及本工作產出，內容校核；不是人工批准或工程簽證'},
        'research': {'status': 'implemented', 'scope': 'Tavily 公開搜尋／網頁擷取與來源引用；需入口啟用、Open WebUI Tavily 設定及本人搜尋權限'},
        'engineering': {'status': 'implemented', 'scope': '需啟用 CAD 入口與隔離映像；DWG 唯讀結構／DXF 讀取、新建 2D 草稿及簡單 TEXT 修改。非視覺或工程尺寸驗證，不重存 DWG'},
        'eip': {'status': 'implemented', 'scope': '真正 EIP 行事曆清單與單筆事項內容、附件查詢（本人可見個人與分享事項）、依明確日期時間準備新增行程 CSV，在 /agents 預覽並本人確認後寫入私人行事曆；不分享不寄信。另有四類公文匣查詢、附件 PDF/DOCX/DOC 同簽核共同分析；本人模型額度、私人 PDF/Word/CSV 下載。公文無期限不自動新增；不簽核或修改刪除既有行程'},
        'webui': {'status': 'implemented', 'scope': 'Open WebUI 可選 Pipe、私人任務頁／下載／確認；登入與附件依伺服器驗證'},
    }
