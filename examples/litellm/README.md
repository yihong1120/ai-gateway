# LiteLLM 設定與部署

本目錄保留 LiteLLM 與 vLLM 的原生 YAML 及公開說明；容器組裝由根目錄 Compose 管理。

| 檔案 | 責任 |
|---|---|
| `config.yaml` | 唯一模型清單、費率、路由與正式政策 |
| 根目錄 `config.example.toml` 的 `[litellm]` | 環境範本；實際值存於忽略的 `config.local.toml` |
| `profiles/dev.yaml` | dev 的原生政策區塊覆寫，模型清單沿用 `config.yaml` |
| 根目錄 `pyproject.toml`、`uv.lock` | 與所有 Python 服務共用的依賴宣告及版本鎖 |
| 根目錄 `docker-compose.yml` | LiteLLM 容器啟動與重啟管理 |
| `local/vllm.example.yaml` | 通用 vLLM 原生後端設定範本 |

雲端專案、區域、端點與憑證路徑採 LiteLLM 原生 `os.environ/NAME` 引用；
數值政策、模型別名、費率與 token 限制在 YAML 配置，不由 Python 覆寫。

## 啟動

```bash
cp -n config.example.toml config.local.toml
chmod 600 config.local.toml
# 填入實際設定後：
ai-gateway compose litellm up -d
```

CLI 選取根目錄唯一的 Compose 及 `litellm` profile，只將 `[litellm]` 投影為服務設定，
並停用 Docker Compose 自動讀取舊 `.env`。
`VERTEX_CREDENTIALS` 使用絕對路徑，Docker 將同一路徑唯讀掛入容器；原生服務也使用此路徑。
完整 Docker profiles 與映像建置入口見 [部署工具](../deployment/README.md#docker-入口)。

正式容器由 Compose 管理，不再另附 LiteLLM systemd 範本。
需要原生模式時，以 `LITELLM_EXECUTABLE`、`LITELLM_CONFIG_PATH`、`LITELLM_HOST`、
`LITELLM_PORT` 在獨立 runtime 前景執行；`ai-gateway exec litellm` 取得中央設定：

```bash
ai-gateway exec litellm -- '{LITELLM_EXECUTABLE}' --config '{LITELLM_CONFIG_PATH}' --host '{LITELLM_HOST}' --port '{LITELLM_PORT}'
```
dev 由既有的 [setup.py](../deployment/dev/setup.py) 一次讀取原生 YAML、合併 dev 政策，
並投影模型所需的環境參數；本機模型的 dev 連線設定來自中央 TOML。
dev 資料庫與管理金鑰各自建立，設定更新後需重新產生 dev 快照並重啟對應服務。

## Gemini 4 模型

三款模型沿用 `[litellm]` 的 `VERTEX_PROJECT`、`VERTEX_LOCATION`、
`VERTEX_CREDENTIALS`，透過 Vertex AI 路由；呼叫時使用下表的公開模型名稱。
先確認專案已開通對應模型，並使用支援的區域（中央範本為 `global`）。

| 模型名稱 | 上游 | 雲端預設思考程度 |
| --- | --- | --- |
| `gemini-4-hydrogen` | `vertex_ai/gemini-4-hydrogen` | `high` |
| `gemini-4-carbon` | `vertex_ai/gemini-4-carbon` | `medium` |
| `gemini-4-argon` | `vertex_ai/gemini-4-argon` | `medium` |

Gemini route 配置模型與供應商連線，不覆寫 `reasoning_effort`、`max_tokens`、
timeout、價格或模型能力。Open WebUI 維持 Default 時，思考程度沿用上表的雲端預設。
Gemini 3.8 與 Gemini 4 使用固定 temperature；路由以
`additional_drop_params: [temperature]` 移除舊客戶端送來的這個參數，避免
1.109.1 回傳 `UnsupportedParamsError`。其餘不支援參數仍採用 LiteLLM 的原有驗證。
輸出預算與請求逾時分別由雲端／LiteLLM 預設或客戶端明確參數決定。
移除 `max_tokens` 不等於要求完整 1M 輸出；Gemini 4 的 1,048,576-token 視窗由
輸入、思考與回答共用，超過單段上限時仍需明確輸出預算與 continuation 支援。
目前原生 runtime 1.109.1 支援 continuation 自動接續。
來源：[Hydrogen](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-hydrogen)、
[Carbon](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-carbon)、
[Argon](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/4-argon)。

LiteLLM 1.106.4 可將 `low`／`medium`／`high` 轉為 Gemini thinking level；
此版本尚不能透過 `reasoning_effort: xhigh` 使用 Hydrogen 的額外思考程度。
參考 [LiteLLM Hydrogen 說明](https://docs.litellm.ai/blog/gemini-4-hydrogen)。

2026-11-13 已實際檢查 PyPI 最新正式版 **1.109.1** 的 wheel、
預發版 **1.111.0.dev1** 的 wheel，以及上游 `main` 價格表：

| 價格來源 | Hydrogen | Carbon | Argon |
| --- | --- | --- | --- |
| 1.109.1 隨附的離線表 | 未收錄 | 未收錄 | 已收錄 |
| 上游 `main`／1.111.0.dev1 隨附表 | 已收錄 | 已收錄 | 已收錄 |
| 1.109.1 預設線上載入，實際驗證 | 已載入 | 已載入 | 已載入 |

因此正式版無須升級成預發版也能從線上價格表取得三款費率。
YAML 已移除 Vertex 手動費率，採用 LiteLLM 載入的價格表。
Hydrogen／Carbon 依賴線上表；若改用離線價格資料，須先確認包含這兩款模型，
不能假設 1.109.1 隨附表已具備其費率。
Azure 的五款模型也已收錄於 `azure_ai/...` 價格項目；本設定保留
`model_info.base_model`，以及輸入、輸出、快取三項明確部署費率。
1.109.1 的 SDK 在傳入 `base_model` 後可正確試算，但實際 proxy 移除手動費率後，
Kimi K2.5 的成功請求在 `LiteLLM_SpendLogs` 被記為零；因此不能只依 SDK 試算就刪除
Azure 費率。Vertex 三款 Gemini 4 則已驗證移除覆寫後仍有非零實際費用。
來源：[PyPI](https://pypi.org/project/litellm/)、
[上游價格表](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)。

費率依 2026-11-13 的 global Standard PayGo：
Hydrogen 每百萬 tokens 為 USD 1.50／7.50／0.075（輸入／輸出含思考／快取輸入）；
Carbon 優惠淨額為 USD 2／10／0.10，到 2026-11-30 為止。
Vertex 的 Carbon 優惠透過帳單抵免實現，LiteLLM 用量帳務按優惠後淨額估算。
2026-12-01 起 Carbon／Argon 為 USD 4／20／0.20。
價格隨 LiteLLM 載入的表更新；優惠到期、區域或服務等級變更後，仍須核對載入的費率。
來源：[Google Cloud 價格表](https://cloud.google.com/model-garden/pricing)。

設定修改後，既有 Compose 容器需重新啟動以載入模型清單：

```bash
ai-gateway compose litellm restart litellm
```

dev 需依前述流程重新產生快照。受限 virtual key 若採模型白名單，須將新模型納入
該 key 的允許清單，才會在其 `/v1/models` 中出現。

若 dev 推論出現 `Your default credentials were not found`，檢查由中央 TOML 投影的
dev `litellm.json` 是否包含 `VERTEX_PROJECT`、`VERTEX_LOCATION` 與
`VERTEX_CREDENTIALS`，並確認該憑證檔對服務使用者可讀。只更新 `litellm.yaml`
模型清單不會同步服務環境；應透過 dev setup 的 `model_environment` 投影更新連線設定，
保留 dev 專屬資料庫與管理金鑰，再重啟 `ai-gateway-dev-litellm.service`。
`/v1/models` 可列出模型不代表上游憑證可用，仍需用最小推論請求確認。

## 雲端輸出上限

下表為官方能力參考，不在 YAML 重複宣告，也不是未指定 `max_tokens` 時的預設預算。
需要較長輸出時，由客戶端明確指定符合供應商限制的 `max_tokens`。

| 模型 | 官方最大輸出 tokens |
| --- | ---: |
| Gemini 4 Hydrogen／Carbon／Argon | 1,048,576 |
| Gemini 3.5 Flash-Lite／3.8 Flash | 65,536 |
| Claude Sonnet 5／Opus 5（Vertex） | 128,000 |
| Kimi K2.5／K2.6（Azure） | 262,144 |
| Grok 4.6（Azure） | 128,000 |
| DeepSeek V4 Flash／Pro（Azure） | 128,000 |

DeepSeek 的 Azure 官方 catalog 為 128k；LiteLLM 1.109.1 的
`azure_ai/deepseek-v4-*` 能力表列 384,000，與此雲端部署規格不同。
YAML 不再覆寫該能力資訊；客戶端若明訂輸出預算，仍須遵守 Azure 實際限制。

來源：[Flash-Lite](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-5-flash-lite)、
[Flash](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-8-flash)、
[Sonnet](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/sonnet-5)、
[Opus](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/claude/opus-5)、
[Kimi K2.5](https://ai.azure.com/catalog/models/Kimi-K2.5)、
[Kimi K2.6](https://ai.azure.com/catalog/models/Kimi-K2.6)、
[Grok](https://ai.azure.com/catalog/models/grok-4.6)、
[DeepSeek Flash](https://ai.azure.com/catalog/models/DeepSeek-V4-Flash)、
[DeepSeek Pro](https://ai.azure.com/catalog/models/DeepSeek-V4-Pro)。

## 本機推論

推論容器使用根目錄 `docker-compose.yml` 的 `local-model` profile，不綁定特定模型家族。
先建立不覆寫既有檔案的私人後端設定：

```bash
cp -n examples/litellm/local/vllm.example.yaml vllm.local.yaml
chmod 600 vllm.local.yaml
```

中央 `config.local.toml` 的 `[local_inference]` 只保存五項容器設定：

| 變數 | 用途 |
| --- | --- |
| `LOCAL_INFERENCE_IMAGE` | vLLM 映像版本 |
| `LOCAL_INFERENCE_MODEL_PATH` | 主機模型目錄 |
| `LOCAL_INFERENCE_CONFIG_PATH` | 私人 `vllm.local.yaml` 的絕對路徑 |
| `LOCAL_INFERENCE_GPU_ID` | 使用的 GPU |
| `LOCAL_INFERENCE_HOST_PORT` | 主機推論埠 |

模型、`served-model-name`、工具／推理 parser、chat template、context／batch／KV cache
與顯存參數直接寫在 vLLM 原生 YAML；它是後端設定資料，不再建立 Python 參數映射層。
依選用模型調整，不把 Gemma parser 或資源限制當作所有模型的預設。
Compose 將主機模型目錄掛在 `/models/model`；YAML 的 `model` 使用此容器路徑。
保留範本的 `host: 0.0.0.0`、`port: 8000`，主機公開埠由上表設定。
原生設定格式見 [vLLM 文件](https://docs.vllm.ai/en/v0.31.0/configuration/serve_args/)。

```bash
ai-gateway compose local-model up -d
```

LiteLLM 的上游連線仍屬 `[litellm]`，使用 `LOCAL_MODEL_API_BASE`、`LOCAL_MODEL_API_KEY`
與開發覆寫的 `LOCAL_MODEL_DEV_API_BASE`；不把 gateway master key 或雲端憑證交給推論容器。
不同推論實例使用獨立設定檔與 project，例如
`ai-gateway --config config.second.local.toml compose --project second-model local-model up -d`。
`served-model-name` 應與 catalog 的 `litellm_params.model` 去掉 `openai/` 後一致：
若使用範本的 `local-model`，該 route 應設定為 `model: openai/local-model`，
並依模型實際容量設定 `model_info`。現有公開 alias `gemma-4-12b`
及上游 ID `gemma-4-12B-it-exl3-4.00bpw_mul1` 保持相容，通用部署不自動改動消費端權限。

私有入口使用 [Nginx server 片段](../deployment/nginx/local-inference-upstream.conf.example)，
由主機既有 Nginx 的 `http` 區塊 include。預設只聽
`127.0.0.1:5002`、允許 loopback，後端為 `127.0.0.1:5003`，僅公開 models／chat API。
依目標主機調整監聽 IP、來源 allowlist 與 upstream，再放到既有 `http` include 目錄。
若 `5002` 已由舊的獨立 Nginx 監聽，先核對該 instance 的用途及管理方式；
切換時由操作者退役原 instance，確認埠已釋放後，才 reload 載入新片段的既有 Nginx。
例如主機已載入 `/etc/nginx/conf.d/*.conf` 時：

```bash
sudo install -m 0644 examples/deployment/nginx/local-inference-upstream.conf.example /etc/nginx/conf.d/local-inference-upstream.conf
sudo nginx -t
# 語法檢查通過後，透過既有 Nginx 服務 reload：
sudo systemctl reload nginx
```

此片段不包含獨立的 `events`／`http` 包裝，也不另啟動一個 Nginx 程序或推論 proxy unit。

切換既有部署時，核對容器 project、模型選項與現有 Nginx 監聽，避免重複佔用同一個埠；
更新來源範本不會自動切換主機服務。
Gemma 的歷史部署與容量證據見 [部署背景](../../docs/gemma4-remote-inference.md)。

## Runtime 相容性

原生 LiteLLM runtime 需要 Python 3.12 以上；依根目錄 `litellm` group 安裝，
與應用程式環境分開。

目前版本鎖固定 LiteLLM 1.109.1、proxy extras 0.4.113、Prisma 0.15.0 與相符依賴。
完整解析以根目錄 [uv.lock](../../uv.lock) 為準；隔離安裝方式見 [開發指南](../../CONTRIBUTING.md)。
dev 的固定版本 Prisma 相容處理與 client 生成集中在既有 [setup.py](../deployment/dev/setup.py)，
於初始化獨立環境時完成。版本變更仍需在隔離環境重新驗證。
前景命令使用已完成初始化的原生 runtime；手動在 dev setup 之外安裝時，
須自行完成對應版本的 Prisma client／執行環境初始化及驗證。

版本切換前備份資料庫、設定、Key／budget 政策及服務入口，先用隔離資料庫驗證。
一般程式回復保留現行資料庫，避免倒灌新增用量與金鑰撤銷；回復步驟使用目標主機
保存的對應發布收條，不以 README 內的歷史主機路徑操作。

## 驗證與紀錄

[WebUI smoke](../openwebui/README.md#手動-smoke-驗證) 驗證登入者模型清單、聊天串流及選用帳務核對；
[dev verify](../deployment/dev/README.md) 核對隔離環境的模型清單、資料庫 readiness、
全域並行政策與 OIDC 導向。既有預算工具、[診斷工具](../deployment/diagnostics/README.md)及
Codex 驗收各自保留原有範圍；實際推論會使用模型額度，需明確執行。
原獨立 LiteLLM 升級 smoke 已退休，這些分工不等同於其完整協定矩陣或最大並行容量驗證。

歷史部署證據見 [LiteLLM 驗證紀錄](../../docs/litellm-1.106.4-production-validation-2026-10-27.json)
及 [Open WebUI 升級紀錄](../../docs/openwebui-upgrade-0.12.4-2026-10-27.md)。
舊模型遷移、未啟用 Word MCP 與主機專屬升級工具已移除；本目錄是工作區部署來源，
不隨應用 wheel 安裝。設定格式參考 [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs)。
