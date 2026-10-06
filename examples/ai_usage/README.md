# Visionnaire 個人 AI 用量 API

這是一個獨立、唯讀的 FastAPI 範例，目錄與
`examples/db_management` 一樣分為 `app`、`routers`、`schemas`、
`services` 與 `deps`。它提供 Visionnaire 前端的「我的 AI 用量」頁面，
但不讀取 Visionnaire、Keycloak、Open WebUI 或 LiteLLM 的資料表。

對外獨立路徑與內部流向如下：

    Visionnaire Browser
        -> Nginx /hazard/api/ai_usage/
        -> Visionnaire BFF (127.0.0.1:8005)
        -> ai_usage (127.0.0.1:8012)
        -> LiteLLM (127.0.0.1:4000)

## 提供的端點

API 服務本身的端點是：

`GET /v1/me/usage/daily?record_limit=50`

Visionnaire 前端使用的同源公開路徑則是：

`GET /hazard/api/ai_usage/v1/me/usage/daily?record_limit=50`

- 直接呼叫 loopback 的 `/v1/...` 端點時，必須帶有經 Keycloak 驗證的
  Bearer access token；Flutter 不可直接呼叫這個 loopback 位址。
- Flutter 應呼叫同源的 `/hazard/api/ai_usage/...` 公開路徑，使用既有
  Visionnaire BFF session cookie；前端不得傳送或保存 Bearer token。
- API 從已驗證 token 的不可變 `sub` 取得身份；沒有任何 `user_id`、email、
  username 或 `end_user` 查詢參數可由呼叫端選擇。
- 只會向 LiteLLM 呼叫 `/spend/logs/v2?end_user=<verified-sub>`。
- 顯示目前 `Asia/Taipei` 當日的總計、雲端／本地模型次數、input/output/cache
  tokens、各模型花費、最多指定筆數的近期紀錄，以及下一次重設時間。
- 回應不含聊天內容、prompt、API key、LiteLLM base URL、原始 OIDC `sub`、
  其他使用者資料或 LiteLLM 原始 metadata。

回應格式範例：

```json
{
  "period": {
    "date": "2026-08-29",
    "timezone": "Asia/Taipei",
    "started_at": "2026-08-29T00:00:00+08:00",
    "reset_at": "2026-08-30T00:00:00+08:00"
  },
  "budget": {
    "limit_usd": 4.0,
    "used_usd": 0.018254,
    "remaining_usd": 3.981746,
    "is_exhausted": false
  },
  "totals": {
    "requests": 12,
    "cloud_requests": 8,
    "local_requests": 4,
    "prompt_tokens": 12000,
    "completion_tokens": 3200,
    "cache_read_input_tokens": 800,
    "total_tokens": 15200,
    "spend_usd": 0.018254
  },
  "models": [],
  "records": [],
  "record_count": 12,
  "records_truncated": false
}
```

`records` 的內容與模型彙總一樣只含計量欄位，不會回傳 prompt 或 completion。
`record_limit` 僅縮短畫面上的近期紀錄；費用與剩餘額度仍由整個當日的資料計算。

## 設定

```bash
cd /home/changdar/Documents/ai-gateway
cp examples/ai_usage/.env.example .env.ai_usage
chmod 600 .env.ai_usage
```

填寫 `.env.ai_usage` 後，至少確認：

1. `LITELLM_USAGE_READER_KEY` 是一把**新建或專用** LiteLLM virtual key，僅允許
   `/spend/logs/v2`，沒有預設模型權限；它不是 LiteLLM master key。
2. `AI_USAGE_DAILY_BUDGET_USD=4.00` 與 LiteLLM 中
   `openwebui-daily-usd-4` 的 `max_budget` 一致。
3. OIDC issuer/JWKS/audience 要與 Visionnaire BFF 目前轉送的 Keycloak access
   token 一致。這個服務只驗證 token，不需要 Keycloak client secret。
4. `AI_USAGE_LOCAL_MODEL_NAMES` 包含所有本機部署的顯示名稱。分類只影響 API
   的本地／雲端標示；實際扣款一律以 LiteLLM 的 `spend` 記錄為準。

安裝與本機啟動：

```bash
conda activate llm_env
pip install -r requirements.txt
python -m uvicorn examples.ai_usage.app:app --host 127.0.0.1 --port 8012
```

`AiUsageSettings` 會自動讀取 repository root 的 `.env.ai_usage`；手動啟動時
不必再 `source` 該檔案。systemd 範本也以同一個檔案作為 `EnvironmentFile`。

提供的 `ai-gateway-ai-usage.service.example` 可作為 systemd user service 的
範本。服務預設只監聽 `127.0.0.1`；Nginx 的公開路徑會先進 Visionnaire BFF，
不得直接反向代理到 `8012`。

## Visionnaire BFF 與 Nginx 串接

在真正部署前，將 Visionnaire 的 `examples/bff/proxy.py` 加入一個 allow-list
upstream（不要讓前端指定 URL）：

```python
_AI_USAGE_UPSTREAM = os.getenv(
    'BFF_AI_USAGE_API_URL',
    'http://127.0.0.1:8012',
)

BFF_UPSTREAMS = {
    # ...既有 service...
    'ai_usage': _AI_USAGE_UPSTREAM,
}
```

實際執行的 Visionnaire BFF 原始碼也要套用相同變更並重新啟動。接著將
[`infra/nginx/hazard-ai-usage.conf`](../../infra/nginx/hazard-ai-usage.conf)
的 `location` 放入既有 HTTPS `server` 區塊；可放在通用的
`location ^~ /hazard/api/` 前方以便維護。Nginx 會將公開路徑轉為 BFF 的
內部路徑，並保留瀏覽器的 BFF session cookie。

因此 Visionnaire Web 只需呼叫獨立公開路徑：

```text
GET /hazard/api/ai_usage/v1/me/usage/daily?record_limit=50
```

BFF 已經持有並轉送 server-side Keycloak access token；Nginx 不應接受或設定
瀏覽器提供的 `Authorization` header。瀏覽器不會取得 token，也不會看到 LiteLLM
reader key。新 API 會再次驗證 Keycloak JWT 的簽章、issuer、expiry 與 audience，
才使用其 `sub` 查 LiteLLM。

## 失敗行為與資料正確性

- LiteLLM 不可用、reader key 權限不足、或回應格式不正確時，回傳
  `503 ai_usage_upstream_unavailable`，不洩漏上游錯誤內容。
- 若單一使用者當日紀錄超過 `AI_USAGE_MAX_LOG_PAGES`，回傳
  `503 ai_usage_data_incomplete`，而不是錯誤地顯示較高的剩餘額度。
- API 只可查詢已帶 `end_user=<Keycloak sub>` 的新紀錄。舊的未歸屬 LiteLLM
  紀錄不會被猜測性地回填給任何帳號。
- Keycloak 修改 username、email、密碼或群組不會拆分歷史用量；停用／刪除帳號
  會停止後續 access token 的使用，但 LiteLLM 稽核紀錄依留存政策保存。
