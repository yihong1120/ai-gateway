# AI 用量 API

唯讀 FastAPI 服務，提供個人用量及可選的管理查詢。
計量來自 LiteLLM spend logs；管理查詢另以唯讀帳號取得本地 tenant、group 與帳號對應，
不直接讀取 LiteLLM、Open WebUI 或 Keycloak 資料表。

```text
Browser → Nginx /hazard/api/ai_usage/ → BFF → AI Usage :8012 → LiteLLM :4000
```

## 啟動與串接

先依 [開發指南](../../CONTRIBUTING.md) 安裝對應環境，再準備本服務設定。

```bash
cp -n config.example.toml config.local.toml
chmod 600 config.local.toml
uv run --frozen --no-dev --extra ai-usage ai-gateway exec ai_usage -- python -m uvicorn examples.ai_usage.app:app --host 127.0.0.1 --port 8012
```

應用載入中央設定的 `[ai_usage]`，不再自動載入散落的 env。
公開範本見 [config.example.toml](../../config.example.toml)；systemd 範本見
[ai-gateway-ai-usage.service.example](../deployment/systemd/ai-gateway-ai-usage.service.example)。

- `LITELLM_USAGE_READER_KEY`：僅允許 `/spend/logs/v2` 的專用 key，不使用 master key。
- `AI_USAGE_DAILY_BUDGET_USD`：與 LiteLLM 的實際使用者每日預算一致。
- issuer／JWKS／audience：與 BFF 轉送的 access token 一致，不需要 IdP client secret。
- `AI_USAGE_LOCAL_MODEL_NAMES`：設定本機模型分類；費用仍以原始 `spend` 為準。

BFF 已有 `ai_usage` allowlist，目標由 `BFF_AI_USAGE_API_URL` 設定。
Nginx 設定見 [hazard-ai-usage.conf](../deployment/nginx/hazard-ai-usage.conf)。
既有網站需要同時支援原生 Bearer 與瀏覽器 BFF 時，路由檢查／套用工具統一在
[deployment/mobile_route.py](deployment/mobile_route.py)，操作限制與搬移說明見
[部署文件](deployment/README.md)。
瀏覽器沿用 BFF cookie，呼叫 `/hazard/api/ai_usage/...`；不保存 Bearer token 或 reader key。
API 驗證 BFF 轉送的 JWT，loopback API 不直接公開給瀏覽器。

## WebUI 本人用量

WebUI Pipe 與本服務共用 `UsageQuerySettings`、`LiteLLMUsageClient.daily_usage(subject)`，
沿用完整讀取、owner 核對與聚合規則；Markdown 由 `presentation.render_daily_usage` 產生。
核心只接收 subject，request 留在 WebUI adapter 邊界。

`adapters/webui.py` 在 app factory 固定設定，lifespan 管理共用 httpx client。
沒有程序全域 reader、每次請求的備援 client，或繞行公開用量 API 的 HTTP 呼叫。
WebUI 在自己的區塊配置受限 reader：

```toml
[webui]
LITELLM_USAGE_READER_KEY = "REPLACE_WITH_RESTRICTED_USAGE_READER_KEY"
LITELLM_USAGE_BASE_URL = "http://127.0.0.1:4000"
LITELLM_DAILY_BUDGET_USD = "4.00"
# AI_USAGE_LOCAL_MODEL_NAMES = "local-model"
```

上述名稱沿用既有 WebUI 環境，顯示預算應與 LiteLLM 的實際 budget 一致。
WebUI 不載入 `[ai_usage]` 的整份 API／OIDC 設定；也不使用 master key。
頁數或資料不完整時顯示待確認提示，不提供部分餘額。
費用以 LiteLLM recorded spend 為權威，價格政策仍在 `examples/litellm/config.yaml`。

既有部署應在宿主 Python 環境透過 pip 安裝新版 repo 應用、更新 Function 原始碼並使用
[WebUI canonical app factory](../openwebui/README.md#啟動)。Function ID／檔名不變；
這些原始碼變更不會自動部署或重啟宿主。

## 個人用量

`GET /v1/me/usage/daily?record_limit=50`

身分只取自已驗證 token 的不可變 `sub`，不能指定其他 user／email／end_user。
查詢當地今日帳務，以 `end_user` 限制 LiteLLM 紀錄。回應包含 period、budget、
總額、模型統計與近期明細；不含 prompt、completion、key、原始 metadata 或 OIDC sub。
`record_limit` 只限制明細，預算與總額仍由完整當日資料計算。

## 管理用量

`GET /v1/admin/usage/{capabilities,groups,summary,users,records,unattributed-records}`

設定 `AI_USAGE_ADMIN_DATABASE_URL` 指向現有帳號 PostgreSQL，以及相同的
`AI_USAGE_OIDC_IDENTITY_PROVIDER`。不需新增資料表或 migration；使用專用唯讀帳號：

```sql
GRANT SELECT (id, username, role, tenant_id, group_id, status) ON users TO usage_reader;
GRANT SELECT (user_id, provider, provider_user_id) ON user_identities TO usage_reader;
GRANT SELECT (user_id, family_name, middle_name, given_name) ON user_profiles TO usage_reader;
GRANT SELECT (id, name) ON group_info TO usage_reader;
GRANT SELECT (id, status) ON tenants TO usage_reader;
```

呼叫者與 tenant 必須 active，每次請求重讀角色與 group；只以已驗證的 provider／sub
連結帳號，不以姓名或 email 猜測。super admin 沿用 `ChangDar`（大小寫不敏感）及
管理員角色規則，可查自己 tenant；一般 admin 只可查自己 group。
超出範圍的使用者回傳 404，一般 admin 指定其他 group 回傳 403。
未設定管理資料庫時回傳 `503 ai_usage_admin_not_configured`，個人 API 仍可使用。

- `start_date`／`end_date` 含首尾日，需成對提供，不接受未來日期。
  省略時查今日；`AI_USAGE_MAX_RANGE_DAYS` 預設及最大為 366。
- period 的 `reset_at` 是迄日翌日午夜；只有恰好查今日才回傳每日 budget。
- 組別依目前成員歸屬，含停用但未刪除帳號；轉組後歷史費用也依目前組別顯示。
- `source` 可篩選應用；`user_totals` 與 `service_totals` 分開呈現。
  公文服務費用不算入個人 budget。
- 專用公文 key 的 SHA-256 設定於 `AI_USAGE_OFFICIAL_DOCUMENT_KEY_HASH`，只有匹配
  該指紋的紀錄可依既有 metadata 歸戶；不複製原始 key，輪替時同步維護指紋。

只有專用 gateway tenant 的 super admin、且未指定 group／user 時可顯示完整帳務。
`AI_USAGE_GATEWAY_TENANT_ID` 必須來自實際 deployment，不可任意指定共用 gateway。
已知其他 tenant 的帳務會過濾，不能作為未歸戶資料洩漏。
完整模式的 totals 等於 attributed_totals 加 unattributed_totals，前端依 coverage
區分完整或已歸戶範圍。未歸戶明細不猜測姓名／組別；分類包括 document_service、
identity_not_linked、system_operation、failed_request、missing_identity，失敗請求仍保留費用。
一般 admin 或指定 group／user 的查詢不能存取未歸戶明細。

## 彙總與完整性

個人、管理、來源及模型共用單一累計模型，內部以 Decimal 累計金額，只在輸出時轉換。
summary 不建立整批明細回應物件；明細端點只建立當頁資料。
管理查詢先取得所選區間的 gateway 帳務，再依可見身分集合篩選；各端點獨立查詢，無快取。
reader key 必須可讀取完整相關 logs；API 無法偵測 reader key 本身隱藏的資料。

讀取超過單次容量時分割時間區間，所有分段與分頁共用 `AI_USAGE_MAX_LOG_PAGES`。
LiteLLM 失敗或格式錯誤回傳 `503 ai_usage_upstream_unavailable`；無法完整取得資料時
回傳 `503 ai_usage_data_incomplete`，不以截斷資料顯示完整總額或較高剩餘預算。
管理 API 的成功及錯誤回應均帶 `Cache-Control: no-store`。

## 維護與驗證

歷史 metadata 重分類只接受可核對的證據，不更動金額、token 或 end_user；
工具與回復方式見 [部署文件](deployment/README.md)。歷史部署收據保留在根目錄 `docs/`，
不代表主機目前版本。客戶端契約見 [個人用量交接](../../docs/ai_usage_flutter_handoff.md)與
[管理用量交接](../../docs/ai_usage_admin_flutter_handoff.md)。

```bash
python -m unittest tests.test_usage_aggregation tests.test_usage_attribution tests.test_ai_usage_service tests.test_ai_usage_api tests.test_ai_usage_admin tests.test_ai_usage_reclassify
```
