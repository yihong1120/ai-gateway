# 公司工作助理

安裝、測試與發布統一使用根目錄的 [開發指南](../CONTRIBUTING.md)。

knowledge 角色可使用管理員設定的唯讀 MCP 工具；工具描述、schema 與領域權限由來源服務維護。
在 `COMPANY_AGENT_CONFIG` 的 JSON 設定 `mcp_servers` 與明確工具允許清單才啟用，預設為空；
個人 OIDC 身分、schema 變更與工作恢復規則見[外部工具連接](../docs/external-tools.md)。

Open WebUI 是唯一正式入口。選擇「公司工作助理（文件／Excel／CAD）」提出任務；
在 [/agents](/agents) 查看進度、下載成果及確認操作，在 [/connections](/connections) 管理本人 EIP／NAS 綁定。

聊天提交後立即返回工作編號與連結，背景工作不依賴聊天連線；關閉頁面後仍繼續。工作台可查看進度、下载與取消，取消中的資源會等待清理完成才釋放。接續問題可在前一工作完成前送出，依序讀取已完成的文字答覆、查證來源及附件。公文 PDF 也使用持久化背景佇列，成果檔案逐筆記錄；服務重啟時保留已產生檔案，將執行中的工作標示中斷，不自動重送。

主協調、知識、文件、資料分析、研究、工程圖、EIP 與審核共八個角色，
使用官方 `create_agent`／LangGraph agent loop；主協調透過工具呼叫獨立子 agent，獨立委派與工具由官方 ToolNode 並行執行。
目前 checkpoint 為 graph31；舊成果保留，更新前未完成工作須依既有版本檢查重新開始。

預設同時執行 4 件工作，同一使用者跨聊天依序執行；`max_concurrent_jobs`、`max_pending_jobs`、`max_pending_per_user` 可在伺服器 JSON 調整。其他帳號優先取得自己的執行機會，不會被單一帳號的待處理清單擋住。共用模型及 OCR 設有獨立並行上限；詳見[部署設定](../README.md)。

2026-10-04 本機正式設定採 8 件公司工作並行，共用待處理佇列上限 1000 件、每人 20 件；公文及 EIP 各 8 件，共用模型 16、OCR 2。公司工作與公文對同一帳號依序執行。模型供應商的限流仍需在正式使用中觀察。

單一工作失敗不影響後續佇列；資料庫連線中斷後 worker 自動重連，保留成果並將失去執行者的工作標示中斷，不重送模型或外部操作。NAS 網路不可用時顯示明確限制，上傳附件等可用來源仍可處理。

## 多組 NAS 帳號

在 `/connections#nas` 可新增、命名、測試、更新或解除個別 NAS 連線，每人最多 16 組。
目前各組帳號連向管理員設定的 `COMPANY_AGENT_NAS_URL`；使用者不能指定任意伺服器。
同一個公司助理工作可讀取多組已綁定帳號；新工作由伺服器保存當時所有可用帳號的版本，
資料夾以「連線名稱 / 資料夾」顯示，內部 root 各自獨立，兩組 `home` 不會混用。
帳密加密保存於 `COMPANY_AGENT_DATABASE_URL` 的 `nas_bindings`，金鑰仍為私人
`state_dir/nas.key`；密碼與 NAS session 不交給模型，每次操作依所屬帳號登入並登出，
仍是唯讀。可讀範圍由各 NAS 帳號的實際權限決定，同一 NAS 帳號不能綁給不同 WebUI 使用者。

新增帳號只影響新工作；舊工作及接續工作保留原本的版本清單，不自動擴張權限。
更新帳密或解除其中一組時，該組的舊版本立即失效，舊工作不能改用其他帳號讀取同名路徑。
其餘綁定、EIP、既有私人成果均保留；需要新來源範圍時另開公司助理工作。

`GET /agents/nas` 回傳本人的 `bindings` 清單（`binding_id`、名稱、帳號、版本及可用狀態），
不包含密碼。既有 `POST /agents/nas/bind` 增加可選的 `label`、`binding_id`：
未指定 ID 時新增或更新同一帳號，指定時只更新本人該組連線。`test` 與 `unlink` 接受
`{"binding_id":"…"}`，多組時必須明確選取；單組仍接受舊的 `{}`。
所有操作沿用已驗證本人身分、同站來源與 `X-Company-Action` 檢查，回應不快取；
每人每分鐘一次的登入限制不因新增多組帳號而放寬。

公司助理 schema 5 將 NAS 唯一鍵改為 `(owner,binding_id)`；升級保留既有密文與版本，
單組帳號的既有工作仍能引用原版本。升級前須備份 PostgreSQL 與私人金鑰；
舊版程式不支援此多組 schema，回退需使用升級前備份，不能只更改版本標記。

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
  `pro` 輔助模型使用 Gemini 4 Argon；也可直接填入任一路由，例如 `{"model":"gemini-4-argon","reasoning_effort":"high","max_tokens":8192}`。

EIP 附件分析的 JSON Schema 直接透過 LiteLLM 原生 `response_format` 傳遞，保留來源與日期驗證。

Office 分工由伺服器的 `office_execution_mode` 設定，於工作建立時固定：

- `flexible`：相容原有 hybrid，主協調可直接計算及製作常見 Office 文件。
- `specialist`：hybrid 主協調只保留搜尋、讀取與整合；Excel 計算／產製交 data，Word／PPT／文字產製交 document，reviewer 維持唯讀獨立校核。
- 專責分工必須啟用 `structured_completion=true`，否則在初始化時拒絕，不允許文字回覆繞過子任務產出驗證。
- 專責模式每次委派填唯一穩定 `work_key` 及 `outputs`；唯讀、純統計或審核填 `outputs=[]`。data／document 不得製作清單外檔案，完成時須交回本次實際產製的全部指定檔案。
- 委派前在資料庫原子保留子任務與檔名，阻止相同 key、完全相同任務或同名檔案再次派工。已完成產檔的修訂使用新 key＋`revises_assignments`；未完成的補做使用新 key＋`resolves_assignments`，均引用真實前次委派 ID。
- 同一原生工具呼叫恢復時可重入原 claim，不重建委派；不自動重試結果不明的外部操作。EIP／CAD 沿用各自領域契約。

[一般優化範例](configs/config.optimized.example.json) 與[混合模型候選](configs/config.mixed-candidate.example.json) 現在皆採專責模式；後者只有 data 使用 Argon。production 設定未切換。詳細設計、限制與驗證見[優化設計](../docs/company-agent-optimization-design-2026-10-17.md)。

[data Flash 候選](configs/config.data-flash-candidate.example.json) 與 Argon 候選只差 data 的模型名稱，其餘設定相同。替換測試包含22題直接 data 與4題完整流程，來源、工具、驗收及其他七個角色固定；執行方式見[模型替換評測](evals/README.md#data-角色的模型替換評測)。候選檔案不代表模型已通過替換門檻，也不會自動部署。

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

共用名額、用量歸屬、聊天觀察及交易連線在 `examples/shared/runtime/`；應用組裝入口為
`examples.openwebui.app:create_app`，以 `ai-gateway exec webui -- python -m examples.openwebui.serve` 啟動。Store 的 schema 升級集中於
`runtime/migrations.py`，一般交易使用有上限的共用連線池，worker lease 使用專用連線。
聊天同步以交易內更新的版本號及待同步索引查詢，舊版本回條不會消耗新的待同步進度。

候選設定的可編輯來源為 `configs/presets.json`，執行
`python -m company_agent.configs.build` 產生完整 JSON；加 `--check` 可檢查是否過期。
服務只讀完整 JSON，不在執行時展開設定繼承。v28／v29 範例保存在 `configs/archive/`；
正式設定與歷史評測結果不由產生器覆寫。

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

應用生命週期各自建立工作服務、設定與一般模型連線快照；變更部署設定後需重啟應用。
請求與背景工作透過 app 綁定取得服務，不共用程序全域工作實例。工作失敗回報為持久化的
安全錯誤代碼與訊息，重新啟動後仍可查詢，既有成果繼續依 owner 授權下載。

附件先在私人 `.preparing_<UUID>` 目錄寫入並 fsync，再原子改名為工作來源目錄；
資料庫鎖只涵蓋容量、去重及工作登記。相同訊息先查既有工作，重試不重新落盤。
提交結果不明時保留來源，由 worker 每 5 分鐘至多檢查 100 個舊準備紀錄；
只有超過一天、沒有活躍準備 lease、且 PostgreSQL 沒有對應工作的目錄才回收。
新準備收條存於 `.pending/<namespace>`，不混入長期工作來源；舊版收條採漸進掃描。
每輪最多走訪 100 個項目，掃描位置會延續，無效項目也計入上限。另有 0.1 秒的操作間
耗時預算，已開始的交易或刪除會等待完成；停止 worker 時關閉掃描 iterator。
資料庫無法查詢時保留檔案；舊版未管理的目錄及所有已登記工作來源都不自動刪除。

容量盤點預設使用 PostgreSQL 估算筆數，`rows_kind` 區分 `estimate`、`unknown` 與 `exact`。
需要精確數字時明列資料表，例如
`python -m examples.shared.runtime.retention --scope company --exact-table web_jobs`。
此選項只計數，不清理工作或成果；文件快取清理預覽仍使用實際候選資料。
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
部署工具從安裝套件的 `examples.openwebui.functions` 讀取公司助理入口；
資料庫儲存的 Function、啟動器及套件必須使用一致的新路徑。

設定範本在 [configs/](configs/)；範本不會自動覆蓋正式設定，正式環境的授權、模型及額度仍由部署設定決定。

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


工作流程優化包含成果版本與歷史下載、產檔前交付規格、統計憑據及確定性驗收；提供 delegated／hybrid／chat 三種固定模式與各角色請求時限。設定、適用範圍與遷移說明見[完整設計](../docs/company-agent-optimization-design-2026-10-17.md)。

隔離評測、失敗紀錄與費用核對方式見[評測說明](evals/README.md)。模型替換須以目前版本重跑驗收。

來源與交付可靠度的graph29設計見[新版設計](../docs/company-agent-reliability-v29-design-2026-10-19.md)：原簿保留計畫先核對實讀來源與工作表、產檔及交付重驗來源SHA、原簿修改須有實際edit_spreadsheet回條；未綁定統計的預留符號與明確同列範圍的錯誤對應在計畫鎖定前退回修正。舊checkpoint不能直接以graph29續跑，既有成果仍依擁有權匯出。

本輪Office候選可審閱[Flash設定](configs/archive/config.office-v29-flash.example.json)與[Argon設定](configs/archive/config.office-v29-argon.example.json)，兩份只有data模型不同，固定hybrid＋specialist與完整驗收。這些是待依來源綁定配置的範例；加入範例不會部署、重啟或切換正式服務。付費測試結果與環境限制見[評測說明](evals/README.md)。

最新工作區為graph31，包含明確原簿修改值的型別驗收、版本鏈錯誤回復與單一文字run的CR／LF／tab輸入檢查。[架構、角色與模型](../docs/company-agent-reliability-v31-design-2026-10-19.md)。候選設定另存[Flash版](configs/config.office-v31-flash.example.json)與[Argon版](configs/config.office-v31-argon.example.json)；兩份只有data模型不同，沒有自動套用正式配置。重跑方式與獨立判分見[評測說明](evals/README.md)。

## 公司 Codex／Claude 資源 MCP

`company_mcp/` 以獨立 ASGI程序提供預設停用的 `/company-mcp` Streamable HTTP入口，
沿用本人 NAS／EIP綁定與來源 ACL，完成各 CLI原生公司 OAuth後預設可使用全部有效綁定。
密碼由後端連接器使用；NAS唯讀，公文分析及行事曆寫入仍需本人網站確認。
每次資源 workspace固定綁定版本，撤銷、停權或改綁會阻止續用。

配置、16個工具、MCP與模型憑證邊界、schema 6升級及發布條件，見
[公司 CLI整合設計](../docs/company-ai-codex-design.md)；
[獨立服務驗證](../docs/company-mcp-service-validation-2026-11-15.json)。
本輪僅完成本機實作與候選來源包，尚未部署或替換正式 EXE。
