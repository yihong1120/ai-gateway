# Open WebUI 整合

安裝、測試與發布統一使用根目錄的 [開發指南](../../CONTRIBUTING.md)。

本目錄保留 WebUI 的應用組裝、啟動與 Function 入口；上游 WebUI 由獨立環境安裝。
原根目錄 `openwebui/functions/` 已移至這裡，不保留重複副本。

| 位置 | 責任 |
|---|---|
| `functions/company_agent_pipe.py` | 公司工作助理入口，委派給 `company_agent` |
| `functions/official_document_processor_pipe.py` | 公文處理入口，委派給 `examples.official_document_workflow` |
| `functions/my_daily_usage_pipe.py` | 登入者本人的當日用量與額度 |
| `functions/keycloak_subject_budget_filter.py` | 從可信登入身分取得 Keycloak subject，傳給 LiteLLM 計費 |
| `app.py`、`serve.py` | 組裝產品路由與資源生命週期，統一 WebUI 啟動入口 |
| `smoke.py` | 明確執行的 WebUI 模型清單、登入者串流與選用帳務核對 |

Function 檔案是由 WebUI 載入的原始碼入口；只匯入 `examples.openwebui.functions`
不會自動載入這些檔案，也不會連線、安裝 Function 或執行部署。
各 Function 的 ID、設定及行為沿用原本契約；核心工作流留在各自的產品套件。

`examples.openwebui` 與 `examples.openwebui.functions` 隨 Python 套件發佈。
公司助理部署工具以 `importlib.resources` 讀取已安裝的 Function 原始碼，不再依賴 repo 根目錄。
其他 repo 操作工具使用新的 `examples/openwebui/functions/` 路徑。
B 機專用的舊升級／探測腳本已移除，歷史驗證見
[Open WebUI 升級紀錄](../../docs/openwebui-upgrade-0.12.4-2026-10-27.md)。

公文使用方式見[公文處理說明](../../docs/openwebui-official-document-processor.md)。

## 身分與本人用量邊界

Filter 保留 Valves 與 `body.user` 防偽，使用宿主提供的可信身分投影；
`examples.identity.adapters.webui.verified_user` 延遲呼叫宿主 resolver，取得已驗證使用者。`examples.identity.providers.subjects.linked_subject` 只解析可信
provider links 的穩定 subject；JWT／OIDC／Keycloak 驗證仍由原身份權威負責。

`my_daily_usage_pipe.py` 只在 WebUI 邊界使用 `__request__.app.state.daily_usage`，
將本人 subject 交給用量核心，再以共用 presentation 呈現。
設定由 app factory 建立快照，HTTP client 在 app lifespan 建立及關閉；不逐次建立備援 client。
查詢直接使用 LiteLLM reader，不呼叫公開 AI Usage HTTP API。

`[webui]` 沿用 `LITELLM_USAGE_READER_KEY`、`LITELLM_USAGE_BASE_URL`、
`LITELLM_DAILY_BUDGET_USD`，可另外指定 `AI_USAGE_LOCAL_MODEL_NAMES`。
不載入整份 `[ai_usage]`，也不需要該 API 的 OIDC 設定或 gateway master key。
reader key 需可完整讀取本人 spend logs；紀錄不完整時顯示待確認，不以部分資料計算剩餘額度。
設定與共用查詢責任見 [用量服務](../ai_usage/README.md#webui-本人用量)。

## 啟動

在已安裝 Open WebUI 宿主與本 repo 應用依賴的 Python 環境中，
沿用既有私人環境、`WEBUI_SECRET_KEY` 與 `DATA_DIR` 執行：

```bash
ai-gateway exec webui -- python -m examples.openwebui.serve
```

ASGI factory 為 `examples.openwebui.app:create_app`。組裝入口只安裝一次各產品路由、
排程與資源生命週期；共用基礎由 `examples.shared.runtime` 提供。
公文套件不再持有 WebUI 啟動器或應用轉接。更新現有環境時，必須同步更新 systemd
啟動命令；範本集中在 [openwebui.service.example](../deployment/systemd/openwebui.service.example)，
直接使用上述 canonical 入口，無需另外覆寫 EIP 啟動命令。範本不會自動修改或重啟正在運行的服務。

既有部署需在 WebUI 宿主 Python 環境透過 pip 安裝新版 repo 應用套件，更新儲存的 Function 原始碼，
並使用上述 canonical app factory，才能取得 app-owned 用量服務。Function ID 與檔名維持原樣；
更新來源不會自動安裝套件、部署或重啟服務。

## 手動 smoke 驗證

在已安裝 Open WebUI 宿主與本 repo 應用依賴的本機 Python 環境執行：

```bash
python -m examples.openwebui.smoke --help
python -m examples.openwebui.smoke --base-url http://127.0.0.1:8080 --output /private/webui-smoke.json
```

`--help` 只顯示參數。實際驗證需要已啟動的 WebUI，以及所選 WebUI 資料庫中的既有 operator；
該帳號須已連結 OIDC subject，並具備 `gemini-3.5-flash-lite` 的使用權限及額度。
工具使用目前 session stamp 產生三分鐘的 automation token，驗證 health、需登入的模型清單，
再送出一次實際聊天並確認串流完整結束。它會消耗模型額度，不隨一般離線測試執行，
報告及標準輸出不包含 token 或其他憑證；`--output` 請使用私人目錄。

設定預設讀取中央 `config.local.toml` 的 `[webui]`；`AI_GATEWAY_CONFIG` 可指定中央檔案，
`--web-config /private/config.toml` 可明確選擇同一格式的檔案，也接受既有的 WebUI JSON 部署快照。
設定及快照由共用設定模組讀取。
`--base-url` 是這次要驗證的 WebUI origin，不另建立 WebUI 環境檔。

需要核對本次請求的 LiteLLM 記名與費用時，在同一命令追加
`--llm-config config.local.toml`。它只為這項選用核對讀取同一中央檔案的 `[litellm]`
（也接受既有 LiteLLM JSON 快照），以回應的 request ID 比對帳務紀錄、operator subject 與實際費用。
未指定時只驗證 WebUI 路徑；兩項核對共用中央設定，各自讀取所需區塊。

## 獨立公司 MCP的資源後端

公開 MCP由 `company_mcp.serve` 的獨立程序提供，WebUI不再掛載公開 `/company-mcp`。
WebUI保留本人綁定與確認頁，以及固定的私有 `POST /internal/company-mcp` authority：
每次驗證 loopback來源、專用服務key與本人資源 OAuth assertion，逐次推導 owner並套用原ACL。
此端點必須由 Nginx明確封鎖，不加入任何 BFF／公開轉送白名單。

`[webui]` 保留 IdP introspection secret及原本資料／加密金鑰，新增專用
`COMPANY_MCP_BACKEND_KEY`。獨立 MCP使用者只能取得其專用 `[company_mcp]`設定檔。
兩程序分別啟停；MCP重啟不會重啟 WebUI、公文worker或重新分析已保存的公文。
配置與運行方式見[公司 MCP設計](../../docs/company-ai-codex-design.md)。
