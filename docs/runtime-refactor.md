# 服務邊界與執行契約

本文件說明目前的依賴、資源與資料一致性契約。逐次重構的測試數量、私人暫存報告
及舊啟動路徑不作為操作入口；每次變更以實際驗證報告為準。

## 應用與核心

| 位置 | 責任 |
| --- | --- |
| `examples.openwebui` | WebUI 組裝、啟動、已驗證使用者 adapter 及 Function 原始碼入口 |
| `examples.official_document_workflow` | 公文解析、OCR、文件輸出、待確認續接與 EIP |
| `company_agent` | 持久工作佇列、工具執行、成果與工作台 |
| `examples.shared.runtime` | pool、資源名額、worker 監督、I/O、用量與工作綁定 |
| `examples.shared.config` | TOML 設定讀取、驗證與服務區塊投影 |
| `examples.deployment.cli` | `ai-gateway` 啟動管理、Compose 與舊設定轉存 |
| `examples.identity` | 帳號、認證、session、provider、授權及單一 ORM metadata |
| `examples.bff` | 瀏覽器 cookie／CSRF／導向、token 更新與 HTTP／SSE 代理 |
| `examples.db_management` | 管理路由、宿主播放及媒體生命週期 |
| `examples.ai_usage` | 已授權範圍內的唯讀帳務與完整性檢查 |

WebUI 由 `ai-gateway exec webui -- python -m examples.openwebui.serve` 啟動；組裝入口以 app 為單位安裝路由、
middleware 與 lifespan，不因重複呼叫增加 scheduler。共用 runtime 不反向組裝產品。
可選通知／串流宿主整合只經 `examples.shared.host`；產品核心不匯入 repo 外的 `src`。

服務在建立時固定持有設定與 I/O 依賴，操作只傳入業務資料。
應用各自持有 job service、WorkflowResources、HTTP client 與資料庫 pool scope，
request／背景 task 透過已綁定的範圍取得它們，不使用可變的程序全域服務。

播放路由位於 `examples/db_management/routers/playback.py`，直接使用
`request.app.state.playback`。播放 identity、upstream、media、rendering 與協調操作
位於 `services/playback/`，由管理應用組裝；不由 IdentityResources 建立。

## 公文流程

`DocumentProcessor` 組合 parser、transport、OCR、sources、exports、rendering 與
clarifications；純值解析／渲染不持有請求狀態。WebUI Pipe 只保留宿主 ABI，
核心直接使用公開服務。新增內部 helper 不擴張 Pipe 介面，複製請求不共用 owner／設定。
EIP HTTP 邊界解析 app-owned resources 後直接使用 state／journal，已移除
`ResourceRef`、`STATE`／`JOURNAL` 全域代理及舊 scheduler／account 包裝入口。
背景排程與容量評測都直接建立持有實際依賴的 `AutomaticScheduler`／`AutomaticAccount`。

UploadBatch 與 EIP BatchWorkflow 各自決定來源與授權規則，共用分組結果、行事曆審核
及 CSV 輸出契約。文件成功、行事曆待確認屬部分成功，不因後段失敗丟棄已完成成果。
可重建快取提交失敗只影響快取，不把已產生文件改報失敗。

待確認資料保存在 PostgreSQL，包含 owner／chat 範圍、期限、來源指紋與原子 claim。
續接會驗證來源及期限，只有輸出確定完成才消耗續接；結果不明保留待核對。

已提交工作的版本包含啟用 Function 與安裝核心的來源指紋。背景執行重新驗證本人附件、
授權、設定與版本；程式更新後不以舊授權快照重跑排隊工作。動態模組 registry 追蹤
活躍引用，閒置版本才可淘汰，並同步移除自身擁有的模組註冊。

成果使用穩定輸出 ID、收條與實際內容證據。所有恢復入口核對 owner、來源、種類、
檔名、大小及 SHA-256；結果不明不能當成未寫入，也不能自動重送。
修復只核對儲存物件、收條與成果投影，不執行工作、重新上傳或重送刪除。
刪除核對分為已刪除、殘留、暫時不可用與目標不符。
細節與管理工具見 [輸出收條](output-receipts.md)。

## 工作、附件與訊息同步

附件先在私人準備目錄落盤並同步，再原子改名。短資料庫交易只處理容量、去重、
來源與工作登記，不在共用寫入鎖內寫整批附件。確認尚未登記時清理；提交結果不明
保留準備收條，回收跳過活躍 producer、已登記工作與其他 namespace 的資料。
回收限制實際掃描／處理數量並維持前進；刪除在資料庫交易外完成。

工作結果與聊天 revision 在同一交易更新；取待送訊息使用索引、重試時間與帳號公平取件，
不反覆組合全部歷史結果。dispatcher 與訊息發送器獨立執行。
送達只確認讀取時的 revision，過期回條不能吃掉新進度；送達失敗只重送訊息，不重跑工作。

失敗代碼、安全訊息與工作狀態持久保存；完整診斷交給日誌，不在記憶體無界保留例外。
取消同步 I/O 時等待已送出的交易完成；未知外部寫入不因重試或重啟而自動重播。

## 資源與資料保留

HTTP client 使用 app-owned 固定具名 profile，同名設定衝突明確拒絕。
PostgreSQL PoolScope 追蹤 owner 與活躍交易，共用同一 DSN 的 pool；
關閉其中一個 owner 不提前中斷其他 owner 或已開始的交易。worker session lease
使用專用連線。獨立評測與 CLI 也應明確持有 scope，結束時釋放資源。

持續 worker 由 lifespan 監督，成功 heartbeat 重設故障退避；其他程序持有 lease
屬 standby。資料庫或清理暫時失敗可恢復，但不重播未知外部操作。
共用模型／OCR 名額在真正請求前取得，取消中的同步請求在結束前仍保有名額。

| 資料 | 保留方式 |
| --- | --- |
| 本人成果、來源附件與工作狀態 | 以協調的資料庫／附件／金鑰備份封存 |
| journal、來源 manifest、外部操作收條 | 保留去重與未知結果證據，不因快取清理刪除 |
| 稽核資料 | 依組織政策另行封存 |
| 可重建分析 cache | 先預覽，再明確指定條件分批清理 |

容量預設使用 relation size 與估算筆數，缺少統計標示 unknown；只對指定資料表精確計數。
清理不在匯入期執行，CLI 見 [共用 runtime](../examples/shared/runtime/README.md)。

用量資料先轉為 UsageFact，個人／管理／模型／來源共用單一 Decimal 累計模型。
summary 不建立整批明細模型，明細只建立當頁；超出完整讀取上限回傳失敗，
不把截斷資料當成完整金額。範圍與權限見 [用量服務](../examples/ai_usage/README.md)。

## 身分與帳號動作

認證核心集中於 `examples.identity`，保留單一 Base／metadata 與既有資料表。
依登入模式由本機 token issuer 或設定的 Keycloak／OIDC 管理權威，BFF 保留瀏覽器邊界。
跨 repo 維護責任見 [認證契約](authentication-cross-repo.md)。

AccountRecovery 將一次性 token 的 SHA-256、用途、email、期限、消耗與寄信狀態存於
PostgreSQL；密碼／驗證狀態與 consumed_at 同交易提交，資料庫回滾不吃掉連結。
先鎖 user 再鎖 profile／token；過期、用途／email 不符及重用均拒絕。
Mailer 持有固定設定與具名 client；成功、明確拒絕與結果不明分開保存。
結果不明保留連結、不自動重寄；寄信完成但結果未提交也不視為未寄送。

LocalSessions 以獨立、帶 TTL 的雜湊紀錄與 Lua 管理 refresh 旋轉、family 撤銷及帳號 epoch，
不覆寫整份使用者 JSON token 清單。refresh 重用撤銷該 family；不同 session 不互相覆蓋。
密碼更新與 revocation_pending 同交易提交，Redis 暫時失敗由 worker 重試，
冪等識別避免重複撤銷後來的新登入。固定視窗限流的計數與補 TTL 同一 Lua 完成。

認證 migration 與公文 migration 分開。既有認證資料庫執行
`python -m examples.identity.migrations`；正常 lifespan 核對版本，不自行更新 schema。
新安裝僅在明確啟用 AUTO_CREATE_SCHEMA 時建立 metadata 並 migration。
舊 Redis 一次性連結及本機舊 session 不自動升為新權威紀錄；OIDC／BFF 契約見上述認證文件。

## 發布與驗證

所有 Python runtime 的直接依賴與安裝組合集中於根目錄 `pyproject.toml`，
`uv.lock` 固定解析結果。不同 runtime 仍使用隔離環境；
安裝、測試與發布統一見 [開發指南](../CONTRIBUTING.md)。
環境範本統一為根目錄 `config.example.toml`，私密設定為 `config.local.toml`；
每個程序只載入自己的服務區塊，既有程序環境優先。模型政策與開發 JSON 部署快照
仍維持產品責任，快照不成為另一份手工環境來源。
應用只依賴 `examples.shared.config` 的設定 API，不匯入部署 CLI；
Docker 與一次性轉存由 `examples.deployment.cli` 在明確執行時處理。
候選設定由 presets 產生完整 JSON，執行時不解析繼承，也不覆寫 production 設定。

BFF 發布包由 `python -m examples.bff.deployment <stage>` 產生完整模組與逐檔雜湊，
不維護部署程式副本。更新時依產品流程排空／停止 worker，安裝同一版核心、
Function 與設定，再啟動驗證；不以目錄整理自動部署或套用正式 migration。

完整離線驗證使用 [開發指南](../CONTRIBUTING.md#離線驗證) 的鎖定環境命令。

驗證使用隔離 PostgreSQL／Redis、模型與宿主替身，涵蓋 CLI、乾淨 sdist／wheel、
repo 外匯入及部署產物。評測以共用 sandbox／合成 fixture 執行，不匯入測試案例；
正式 wheel 排除 `company_agent.tests`，測試仍保留在 repo。額外容器、PowerShell、真實供應商及本機歷史封存位元組核對
依執行條件明確回報略過，不宣稱已完成。入口及報告方式見 [部署工具](../examples/deployment/README.md)。
