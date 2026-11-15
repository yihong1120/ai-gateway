# 服務與整合功能

可部署的應用與整合功能集中在 `examples/`，依產品責任分類：

| 目錄 | 責任 | 入口 |
|---|---|---|
| [company_ai_access](company_ai_access/README.md) | AI 裝置註冊、授權與推論 | `examples.company_ai_access.app:create_app`（factory） |
| [ai_usage](ai_usage/README.md) | 個人及管理用量查詢 | `examples.ai_usage.app:app` |
| [db_management](db_management/README-zh-tw.md) | 帳號、租戶、群組、站點與部署管理 | `examples.db_management.app:app` |
| [identity](identity/README-zh-tw.md) | 使用者、登入、token／session 與共用認證資源 | 由服務 lifespan 使用 |
| [bff](bff/README-zh-tw.md) | 瀏覽器登入、session 與代理 | 掛載於管理應用 |
| [deployment](deployment/README.md) | 部署工具與註冊服務 | `registry/` 掛載於管理應用 |
| [official_document_workflow](official_document_workflow/README.md) | 公文解析、輸出、EIP 工作流與驗收 | 由 WebUI 組裝入口安裝 |
| [litellm](litellm/README.md) | 模型代理設定、環境範本、版本鎖與部署工具 | `ai-gateway compose litellm up -d`、原生前景命令 |
| [litellm/local](litellm/README.md#本機推論) | 通用 vLLM 容器與原生後端設定 | `ai-gateway compose local-model up -d`、`local/vllm.example.yaml` |
| [openwebui](openwebui/README.md) | WebUI 應用組裝與 Function 入口 | `ai-gateway exec webui -- python -m examples.openwebui.serve` |
| [shared](shared/README.md) | 設定讀取、跨產品執行基礎與宿主 adapter | `examples.shared.config`、`runtime`、`host` |

管理與用量服務採用 `routers`、`schemas`、`services` 分工；AI 存取服務另有
`storage`、`adapters`、`protocols` 與 `cli`。共用認證模型按領域放在 `identity/models`，
所有服務共用同一個 metadata 註冊入口。

## 程式與資料的分工

| 邊界 | 單一責任 |
| --- | --- |
| `routers/`、`adapters/` | 驗證協定輸入、取得可信身分、轉接宿主與回應 |
| `services/`、`application/` | 業務規則、交易邊界、工作協調與明確的成功／部分成功結果 |
| `schemas/`、`domain/` | 資料契約與純運算；外部資料先驗證再進入流程 |
| 根目錄 `config.local.toml` | 各程序的私人設定；一次讀取後只投影所需區塊 |
| 產品的原生 YAML／公開範本 | 模型 catalog、路由政策及後端選項，由對應產品維護 |
| `deployment/`、`evals/` | 明確執行的部署與驗收工具；不在匯入時執行外部操作 |
| 根目錄 `artifacts/`、`test_outputs/` | 生成的發布包、執行資料與報告，已忽略於 Git |

目錄依實際責任設置，不要求每個產品建立整套空目錄。
跨服務操作用 `examples.shared.config.read_services(...)` 取得同一份設定快照；
各服務只取得自己的環境，不共享管理金鑰。開發環境生成檔使用
`examples.deployment.dev.files.save` 原子寫入，與加密金鑰的首次建立流程分開。
共用設定模組只處理 TOML 讀取、驗證及區塊投影；啟動、Compose 與舊設定轉存由
`examples.deployment.cli` 管理，保留 `ai-gateway` 命令入口。
資料庫的容量檢查與變更由 service 在同一交易內完成，路由不另維護一套寫入規則。
公文輸出以明確結果攜帶已確認檔案；部分失敗及結果不明分開處理，保留已完成成果。

## 安裝與啟動

統一安裝組合、測試與發布方式見 [開發指南](../CONTRIBUTING.md)。
先建立對應服務環境，在根目錄 `config.local.toml` 填寫所選服務區塊，
再以 `ai-gateway exec 服務區塊 -- 命令` 使用上表入口。
FastAPI 入口由 Uvicorn 載入；Company AI 是 factory，WebUI 使用自己的 serve 模組。

這些入口沿用各服務的資料庫、SSO、Redis 與權限設定。套件的搬移不會改名資料表、
API 路徑、資料庫 schema 或既有憑證。

原 `gateway_services.*` 已移除，請使用表中的命名空間；不提供重複的 ORM 註冊別名。
BFF 的組裝入口為 `examples/bff/proxy.py`，完整模組以 `python -m examples.bff.deployment <stage>`
產生身分／BFF 模組 bundle 與逐檔雜湊 manifest。生成目錄使用已忽略的
`artifacts/identity-services`，與來源程式分開；遠端既有部署位置仍由部署腳本指定。

公文／行事曆工具已歸入公文套件：CSV 轉換使用
`python -m examples.official_document_workflow.cli.prepare_calendar_csv`；本機語料與 EIP 附件驗收見
[公文驗收工具](official_document_workflow/evals/README.md)。核心設計見
[runtime 說明](../docs/runtime-refactor.md)。


## 部署工具

服務專屬工具位於各產品的 `deployment/`；共用開發環境、驗證工具與歷史發布包見
[部署工具總覽](deployment/README.md)。Docker 建置與容器組裝只保留根目錄
`Dockerfile`、`docker-compose.yml`、`.dockerignore`；產品目錄保留各自的原生設定資料。
BFF 產物使用
`python -m examples.bff.deployment artifacts/identity-services` 產生。
