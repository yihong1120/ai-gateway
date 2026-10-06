# Company Agent class 與執行層重構驗證

日期：2026-09-10。範圍為 company_agent；不變更每小時 EIP pipeline 的業務規則。

## 已完成

本次修改／新增 company_agent 內 29 個檔案，其中 26 個為非測試 Python 程式。
正式程式與 evals 的函式內函式／class 定義由 45 個降為 0；測試 callback 不計入。
不是以縮短總行數為目標，而是移除 closure 隱含依賴、合併重複實作與分開責任。

| 責任 | 新實作 |
| --- | --- |
| 模型節點 | runtime/reasoning.py 的 RoleNode.invoke；當次 Context／State 留在呼叫內 |
| 工具整批預檢 | runtime/preflight.py 的 ToolPreflight；預檢、標準 ID、唯讀參數修正回條 |
| 額度 | runtime/budget.py 的 check_model_budget；沿用既有步數及收尾規則 |
| 工具建構及回條 | runtime/tool_adapter.py；共用官方 StructuredTool、InjectedToolArg、ToolMessage |
| 委派、角色完成 | DelegationTool／CompletionNode／CompletionToolAdapter |
| 計費驗證 | WebBilledModel.verify_subject／VerifiedModelInvocation，每次呼叫重新查驗 |
| WebUI 服務 | ServiceConfiguration／QueueProgress／SafeRequestHandler／ServiceLifespan |
| EIP callback | 當次處理專用 BilledPipeAdapter、EIPProgress、BindingGuard |
| 檔案／Office／CAD | 當次操作專用 FolderScan、FormulaDependencies、XMLParts、RowPage |
| 評測 | 具名模型呼叫 wrapper 及進度 callback；未執行付費模型評測 |

reasoning.py 原最大函式 role_nodes 為 283 行；拆分後 RoleNode.invoke 為 81 行。
各角色仍以官方 StateGraph／ToolNode 執行；沒有新增 Agent 框架或第三方依賴。
36 個模型可見工具的名稱、說明、完整 schema 與重構前的雜湊基準一致。
GRAPH_VERSION=20、圖節點與邊、State、工具角色範圍及完成協定不變。

## 必要檢查仍保留

保留本人身分／工作歸屬、NAS ACL、來源雜湊、計費與額度、Pydantic 契約、
工具角色範圍、操作去重、人工確認、成果驗收與 checkpoint 恢復。
整批預檢不是 ToolNode 參數檢查的重複程式：必須先確認整批合法，才容許第一項工具產生副作用。
沒有增加自動重試、fallback、外部分享或寫入權限。

共用工具 adapter 只存建構設定，Runtime 由每次呼叫注入。
持有工作資料的 callback、掃描器及 XML cache 則每次操作建立新實例，不跨使用者共用。
class 本身不是隔離保障；原有資料與執行授權仍是隔離邊界。

## 驗證結果

- 全套 Python：**334 項通過，0 項跳過**（132.194 秒）。
- 使用真實 PostgreSQL 測試 schema、Office 容器、本機 OCR，以及本次新建的 CAD image。
- 新增 6 項測試：無巢狀定義、36 個工具契約、無 closure 的具名入口、操作 cache 獨立、
  同一研究工具 adapter 的雙使用者交錯執行、同一完成工具 adapter 的工作歸屬重驗。
- 額外架構／版面／新測試：24 項通過。
- wheel 直接載入：32 項通過；wheel／sdist 建置成功。
- wheel 獨立安裝後，實際 parser 子程序產生 DOCX 並重新抽取文字成功。
- 兩套 Node UI 測試通過：工作台、統一帳號連結頁。
- 上線後管理員與一般帳號 smoke 通過；模型可見、計費身分有效、錯誤參數 400、CSRF 403。
- 健康檢查 200、匿名頁面轉登入 303、六個已登入 UI 資源與套件一致、工作及 EIP 狀態 API 200。

模型、EIP、NAS、Tavily 的業務操作使用測試替身；不代表重新驗收真實 NAS 或 EIP 寫入。
上線 smoke 未提交工作或付費模型呼叫，也未為測試修改真實行程。

## 部署與回復

重啟前再次檢查：公司助理排隊／執行 0、EIP 自動處理中 0、EIP 手動工作 0。
已更新 CAD image 並重啟 openwebui.service；重啟後服務為 active，以上 smoke 已通過。

- 更新前原始碼備份：`/tmp/company-agent-class-refactor-3F7fHj/company_agent/`
- CAD 原 image 保留為：`ai-gateway/company-cad:before-refactor-3f7fhj`
- CAD 新 image：`ai-gateway/company-cad:refactor-3f7fhj`，正式 `:1` 指向此 image。
- 新 image ID：`sha256:041cfc6bd4a66b6d151a566153729405b80e539c0886ada60a0d78e2fb25f9aa`
- 測試、套件、部署檢查紀錄：`/tmp/company-agent-class-refactor-3F7fHj/`

本次沒有刪除使用者工作、來源檔案、帳號綁定、行程或正式資料庫紀錄。
若要回復程式，先確認背景工作已停止，再逐項還原此次變更並改回 CAD image，避免覆蓋後續修改。
暫存備份不是長期備份；正式保留應依公司的版本管理及備份政策處理。
