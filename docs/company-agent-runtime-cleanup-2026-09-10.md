# company_agent/runtime 精簡（2026-09-10）

## 實際改動

runtime 的 Python 模組（含 `__init__.py`）從 26 個減至 18 個；刪除七個被合併的模組，另將進度顯示模組移至 API。沒有保留舊路徑轉接殼。

| 原模組 | 現在的歸屬 |
| --- | --- |
| `agents.py` | `agents/spec.py` 的 `AgentSpec.build_agent`；各角色入口直接綁定 SPEC 方法，不再透過 partial／外部建構器 |
| `tool_adapter.py`、`tool_catalog.py`、`tool_registry.py` | `runtime/tools.py`：官方工具建構、契約及註冊集中管理 |
| `policies.py` | `runtime/preflight.py`：批次政策與批次預檢在同一處 |
| `read_repair.py` | 分頁更正在 preflight、修正回條判讀在 history、公開錯誤在 errors、讀取結果整理在 results |
| `capabilities.py` | `agents/registry.py`：角色註冊與能力目錄放在一起 |
| `progress.py` | `api/progress.py`：使用者介面進度文字不屬於圖執行引擎 |

工具 schema 與 description 改為同一份 `FILE_TOOL_SPECS`，消除兩份字典重複列出工具名稱；需要 schema-only 索引的地方從該定義衍生。工具模型、說明、順序與公開參數不變。

runtime 原始碼 2,539 → 2,405 行。全專案非測試 Python 原始碼 10,013 → 9,948 行，實際淨減 65 行：這個數字已扣除移到 agents／api 的程式，不將搬檔誤算成刪除功能。

## 保留的行為

模型計費、工作／帳號隔離、核准與去重、加密 checkpoint、來源雜湊核對及失敗紀錄維持原有行為。GRAPH_VERSION、State 欄位、節點／邊與工具 schema 均由原本凍結的 v20 測試驗證，不為了通過測試修改基準。

工具註冊工廠在呼叫時匯入角色 adapter，避免 adapter 使用共同工具建構器時形成循環匯入；沒有增加另一個 registry、plugin loader 或相容層。角色建構方法只綁定 SPEC，使用者與工作依賴仍由每次執行注入。

## 測試與還原

測試使用 PostgreSQL 隔離 schema，並啟用 CAD 容器、Office、本機 OCR 整合；模型與遠端業務 HTTP 使用替身，不發出付費模型請求，也不新增 EIP 行程或寫入 NAS。另以 wheel 執行架構／UI／角色／工具契約測試，檢查已退役 runtime 模組確實不存在。

修改前完整程式及測試／部署紀錄：`/tmp/company-agent-runtime-iqaXuo/`。被合併刪除的模組可從該備份還原；沒有刪除聊天、附件或工作資料。

驗收完成：372 項完整回歸（145.125 秒、無略過）、wheel 47 項及兩組 Node 測試全部通過；Ruff F／RUF059 與 Vulture 高可信度掃描無問題。

確認公司助理及 EIP 自動／手動工作均閒置後已更新服務。部署後管理員／一般使用者入口、模型可見性、計費身分、六個套件資源、工作清單及 EIP 狀態正常；健康檢查 200、匿名導向登入 303、無效參數 400、CSRF 防護 403。部署驗收未產生付費模型請求。
