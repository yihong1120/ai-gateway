# 公司工作助理

Open WebUI 是唯一正式入口。選擇「公司工作助理（文件／Excel／CAD）」提出任務；
在 [/agents](/agents) 查看進度、下載成果及確認操作，在 [/connections](/connections) 管理本人 EIP／NAS 綁定。

聊天提交後立即返回工作編號與連結，背景工作不依賴聊天連線；關閉頁面後仍繼續。工作台可查看進度、下载與取消，取消中的資源會等待清理完成才釋放。接續問題可在前一工作完成前送出，依序讀取已完成的文字答覆、查證來源及附件。公文 PDF 也使用持久化背景佇列，成果檔案逐筆記錄；服務重啟時保留已產生檔案，將執行中的工作標示中斷，不自動重送。

主協調、知識、文件、資料分析、研究、工程圖、EIP 與審核共八個角色，
使用官方 `create_agent`／LangGraph agent loop；主協調透過工具呼叫獨立子 agent，獨立委派與工具由官方 ToolNode 並行執行。
目前為 v25；舊成果保留，更新前未完成工作須重新開始。

預設同時執行 4 件工作，同一使用者跨聊天依序執行；`max_concurrent_jobs`、`max_pending_jobs`、`max_pending_per_user` 可在伺服器 JSON 調整。其他帳號優先取得自己的執行機會，不會被單一帳號的待處理清單擋住。共用模型及 OCR 設有獨立並行上限；詳見[部署設定](../README.md)。

2026-10-04 本機正式設定採 8 件公司工作並行，共用待處理佇列上限 1000 件、每人 20 件；公文及 EIP 各 8 件，共用模型 16、OCR 2。公司工作與公文對同一帳號依序執行。模型供應商的限流仍需在正式使用中觀察。

單一工作失敗不影響後續佇列；資料庫連線中斷後 worker 自動重連，保留成果並將失去執行者的工作標示中斷，不重送模型或外部操作。NAS 網路不可用時顯示明確限制，上傳附件等可用來源仍可處理。

## 模型分流

**每個 Agent 固定一個模型**：`role_models` 指定各角色的 LLM、思考等級與輸出上限；
未設定的角色使用 `model_name`。協調者只委派任務與上下文，不能替其他 agent 換模型。
同一角色的不同委派及工作恢復都沿用固定設定。修改模型設定後須另開工作或恢復原設定續跑。

`model_profiles` 僅供 `understand_image` 與 `eip_process_attachments` 等輔助分析工具選擇，
不改變呼叫工具的 agent 模型。設定名稱可自由新增；見 [Gemini 輔助分析模型範本](configs/config.gemini.example.json)。
啟用清單後，子分析的 purpose 只作稽核標籤；未指定 profile 時使用預設模型。
模型清單不授予工具權限，文字模型不能用於圖片分析，不接受清單外模型，也不自動重試。

系統背景的聊天壓縮發生在主協調規劃前，因此仍保留廉價摘要預設，不為了選摘要模型再增加一次 LLM 呼叫。
`purpose_models` 保留舊部署相容；沒有 `model_profiles` 時才沿用舊的子分析用途路由。


`COMPANY_AGENT_CONFIG` 指向伺服器端 JSON 設定檔。`model_name` 為未指定路由時的模型，
`COMPANY_AGENT_MODEL` 環境變數可覆寫它；`role_models` 與 `purpose_models` 不受此覆寫影響。
各路由支援 `model`、`reasoning_effort`（省略時交給供應商）及 `max_tokens`（預設 4096）。

- `role_models`：coordinator、knowledge、document、data、reviewer、research、engineering、eip。
- `purpose_models`：summary（聊天摘要）、vision（圖片語意）、attachment（EIP 附件解析）、attachment_plan（附件處理指令規劃）。
- 未填的路由沿用預設模型；未指定 summary 路由時，摘要輸出上限維持 2048。
- [Gemini 範本](configs/config.gemini.example.json) 與[本機正式配置](configs/config.production.json) 的八個角色均固定使用 `gemini-3.5-flash-lite`／`medium`／4096；摘要使用同模型的 `minimal`／2048。
- 圖片及 EIP 附件子分析預設沿用 Lite；可選清單僅有 Gemini `lite`、`flash`、`pro`，一般工作優先 Lite，確需較強能力時才明確選用其他輔助模型。這不改變呼叫工具的角色模型，也不提供失敗後自動切換。
  Pro 可直接填入任一路由，例如 `{"model":"gemini-3.1-pro-preview","reasoning_effort":"high","max_tokens":8192}`。

EIP 附件分析的 JSON Schema 直接透過 LiteLLM 原生 `response_format` 傳遞，保留來源與日期驗證。

所有路由仍使用本人同一個已驗證的 LiteLLM 身分及工作步數額度；不會失敗後偷偷換模型或重送工具。
模型、思考等級與輸出上限會記錄在新工作的 checkpoint；設定改變時，舊工作須以原設定續跑或另開工作。
舊版未記錄路由的工作不允許直接套用新分流繼續。修改 JSON 對下次工作生效；程式更新需重啟 WebUI。

Gemini 的工具 schema 僅將 `maxItems` 改寫為說明文字，原始 Pydantic 工具上限仍在執行前驗證。
思考簽章在傳回 Gemini 時保留，內部工具回條仍使用唯一的正規化 ID。
不同模型的名稱必須已存在於 LiteLLM 且在使用者金鑰的允許清單中。
這些設定只影響公司助理，不修改獨立公文 PDF Pipe 的模型設定。

## 目錄

```text
company_agent/
├── README.md
├── __init__.py
├── graph.py                 # 官方 create_agent 入口
├── state.py                 # 工作狀態與 reducer
├── config.py                # 設定契約
├── schemas.py               # 共用 schema 基底
├── agents/                  # 八個角色：SPEC、提示詞、專屬工具函式
├── services/                # 檔案、Office、CAD、研究、EIP 業務
├── runtime/                 # 身分、模型、工具執行、計費、儲存與恢復
├── api/                     # WebUI pipe、工作台路由、身分整合、工作佇列
├── ui/                      # HTML／JS／CSS
├── configs/                 # 設定範本，不是正式設定
├── ops/                     # 管理員部署、服務檢查
├── evals/                   # 合成任務及模型評測
└── tests/                   # 自動化回歸測試
```

不保留舊模組轉接層。舊管理員 CLI 與一次性的 SQLite 切換工具已移除；
查詢、下載、接續及確認操作統一由 WebUI 提供。

工作持久化與查詢集中於 `api/queue.py`，背景執行與設定在 `api/service.py`。
工作台清單與狀態共用單次查詢，背景工作共用 Store，模型呼叫計數由資料庫原子更新。

## 功能與界線

| 領域 | 已提供的能力 |
| --- | --- |
| 檔案與 NAS | 依本人授權搜尋與閱讀、來源版本核對；Synology 連接器維持唯讀 |
| Office | Word／PPTX 草稿、指定文字修改、Excel 讀取與原簿修改、受限公式及圖表 |
| 圖片與工程圖 | 本機 OCR、圖片語意理解、DWG／DXF 結構分析及受限 DXF 草稿／文字修改 |
| 研究 | Tavily 搜尋、網頁擷取、來源及查詢時間 |
| EIP | 本人文件匣與行事曆查詢、附件分析、產出文件、行程準備及本人確認橋接 |
| 工作管理 | 私人成果、審核、去重、額度限制、PostgreSQL checkpoint 及中斷恢復 |

不提供任意程式執行、任意 DWG 重存、完整工程安全判定或 EIP 簽核。
文字／數值校核不等於視覺排版合格；不應將草稿視為公司正式核定文件。

公司助理與每小時 EIP 公文自動處理是不同入口。來源沒有可確認日期時不憑空新增行程；
公司助理的外部寫入遵循 WebUI 的授權及確認流程。

## 帳號與計費

登入身分由 WebUI 伺服器驗證，不信任模型或瀏覽器自行指定的 owner。
各使用者、工作與委派的模型上下文由獨立子圖隔離；角色共用本人已驗證的 LiteLLM 計費身分，
不另建每個 Agent 的額度，也不改用公文免扣額度金鑰。

PostgreSQL 保存工作及成果；checkpoint blobs 加密，不能據此宣稱全庫資料皆加密。
備份需包含資料庫、私人來源附件及 checkpoint 金鑰。不要因整理程式目錄而移動或清除正式資料。

Open WebUI 是聊天歷史與分支的唯一來源。Pipe 從本人已儲存的目前分支擷取對話，
不接受請求 body 中自帶的歷史、系統訊息或工具回條；佇列只保存本次執行輸入快照，
LangGraph checkpoint 保存該工作的摘要、工具紀錄、確認與續跑狀態，不回寫 Open WebUI 聊天歷史。
主協調讀取分支背景；子 Agent 只取得本次指令、委派與所需摘要。

本次附件使用 `uploads`，同分支既有公司助理工作的來源使用唯讀 `history_N` 根目錄。
原始附件不再逐輪複製；資料庫成果首次沿用時在原工作目錄建立一份私人快取，後續驗證雜湊並重用。
不會掛載其他聊天室或分支的工作目錄；其他模型處理的歷史附件若未進入公司助理，仍須重新選取上傳。
入口用 Open WebUI 的原生函式選取目前分支及已儲存的摘要，仍保留完整分支的附件參照。
工作開始時，聊天背景達 `summary_trigger_tokens`（預設約 16000 token），交由官方
`SummarizationMiddleware` 決定摘要邊界，保留約四分之一門檻的近期背景與最新指令。
摘要直接存入 LangGraph checkpoint；模型或核准續跑沿用已完成節點，不重新產生摘要。
不再自行切段、建立跨工作摘要快取或每次呼叫重新組裝壓縮結果；新工作必要時重新摘要。
token 使用官方近似計數器的保守文字設定，不等於 LiteLLM 實際帳單，原聊天仍在 Open WebUI。
摘要使用 `purpose_models.summary` 與本人計費身分，計入工作步數；供應商例外直接拋出，沒有自動重試。
因 LangChain 1.2.10 預設會把摘要失敗轉為文字，保留一個呼叫介接覆寫此行為；沒有自訂摘要品質檢查。
摘要輸入不自行裁切；過長內容交由供應商的上下文限制處理。工作開始後的工具回條不摘要，
保留成果與審核證據。聊天分支輸入仍有 200 萬字上限。歷史對話不是新的外部寫入授權。

## 維運

在原始碼工作區及已配置的服務環境中，由管理員明確執行：

```bash
python -m company_agent.ops.deploy_webui
python -m company_agent.ops.smoke_webui
```

第一項統一備份並更新 Pipe、模型與三個自有首頁入口，不是診斷命令；
第二項只做讀取與無效請求防護檢查，不送出生成工作。不再保留獨立的 deploy_connections 指令。
部署工具需要工作區中的 openwebui/functions/company_agent_pipe.py；
資料庫儲存的 Function、啟動器及套件必須使用一致的新路徑。

設定範本在 [configs/](configs/)；範本不會自動覆蓋正式設定，正式環境的授權、模型及額度仍由部署設定決定。
如需安裝開發環境，可在儲存庫根目錄執行 python -m pip install -e '.[company-agent]'。

## 測試與評測

```bash
python -m unittest discover -s company_agent/tests -t .
python -m unittest tests.test_company_webui_entry
node tests/test_company_agent_dashboard.cjs
node tests/test_company_connections.cjs
uvx ruff check company_agent --select F,RUF059
```

完整資料庫測試需 COMPANY_AGENT_TEST_DATABASE_URL；每個測試建立並清理自己的隨機 schema，
不使用正式 schema。另設 COMPANY_AGENT_CAD_TEST=1、COMPANY_AGENT_OFFICE_TEST=1、
COMPANY_AGENT_OCR_TEST=1 啟用容器及本機 OCR。略過整合測試時不能宣稱完整驗收。

evals 下的工具供明確授權的合成測試使用，不是正式入口；部分工具會消耗模型或 Tavily 額度。
範例與限制見 [評測說明](evals/README.md)。一般回歸測試使用模型／遠端服務替身，不會自動轉成付費實測。

工具批次交給官方 ToolNode 並行執行，沒有額外批次白名單、筆數上限或開關；舊設定 `allow_safe_tool_batches` 已移除。模型依工具之間的資料依賴安排呼叫，需先取得來源版本或附件 handle 才能執行後續操作。提示詞要求存檔申請與完成工具各自單獨呼叫；實際存檔仍須本人核准，完成仍須驗證所有產出與審核證據。同批工具不是交易，任一項失敗直接拋出，其他項目可能已完成；衝突的 state 更新由 LangGraph 處理。原有工作步數、各服務額度與本人權限繼續適用。

## 自主跨來源查證

主協調依問題需要選擇網頁、本人可讀 NAS／附件、EIP 及 CAD，亦可加入 Excel 計算。獨立子任務可同批委派，由 ToolNode 並行呼叫獨立子圖，每次委派有自己的訊息與 checkpoint；角色內的獨立工具也可並行執行。有依賴的查證按結果接續，例如先從 NAS 找圖，再由工程圖角色讀 CAD。不是每題固定查全部來源。

專責角色可在結構化完成回條的 `evidence_requests` 提出其他來源的唯讀補證需求，主協調依原問題判斷與委派；建議本身不會自動執行或授予權限。最終回答保留來源、版本、衝突與缺口；必要來源不可用時交付已有發現並說明未完成部分。內部內容不因跨來源整合而送到公開搜尋，存檔與行事曆寫入仍須既有確認。
