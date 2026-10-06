# Visionnaire AI 用量 API：Flutter 交接規格

適用於 Visionnaire Flutter Web、iOS 與 Android。此 API 只顯示「目前登入
使用者」在當日的 AI 用量與剩餘額度；前端不能指定其他人的資料。

## 上線前提

後端完成下列項目後，前端才能串接：

1. AI Usage API 持續在 server 的 `127.0.0.1:8012` 運行。
2. Visionnaire BFF 的 allow-list 有 `ai_usage -> http://127.0.0.1:8012`，且
   port `8005` 已重新啟動。
3. Nginx 已發布 `/hazard/api/ai_usage/` 到 BFF。

前端絕不可使用 `http://127.0.0.1:8012`：在手機／使用者電腦上，`127.0.0.1`
指向的是該裝置本身，不是 server。

## 唯一可使用的 API

```text
GET https://changdar-server.mooo.com/hazard/api/ai_usage/v1/me/usage/daily?record_limit=50
```

- `record_limit`：可省略，預設 `50`；有效範圍為 `1` 到 `100`。
- 不可傳送 `user_id`、email、username、Keycloak `sub`、日期、模型或 LiteLLM
  篩選條件。
- 資料永遠是 `Asia/Taipei` 的「今天」；日界是台灣時間午夜。
- 回應帶 `Cache-Control: no-store`，前端不可寫入本機持久快取或 analytics log。

## 身份驗證：所有平台都使用 BFF session

此公開路徑的流向是：

```text
Flutter App -> /hazard/api/ai_usage/ -> Visionnaire BFF -> 127.0.0.1:8012
```

BFF 以 HttpOnly、Secure 的 `__Host-vn_session` cookie 驗證登入狀態，並由
server-side Keycloak token 查詢對應的 OIDC `sub`。因此：

- 不要在此 API 附加 `Authorization` header。
- 不要把 Keycloak access token、LiteLLM key 或 end-user ID 放進 Flutter。
- Web 請使用相同 origin 的相對路徑，例如
  `/hazard/api/ai_usage/v1/me/usage/daily`；瀏覽器會帶上 session cookie。
- iOS／Android 必須使用既有的 Visionnaire BFF 登入流程與同一個可保存 cookie
  的 HTTP client／cookie jar。若 native app 僅在外部瀏覽器完成 OIDC 登入、但
  Dart HTTP client 沒有 BFF session cookie，不能直接呼叫此 API；請先由後端
  團隊提供 native BFF session 建立流程，不能改為直接連 `8012`。

這支 GET API 不需要 CSRF header。

## Flutter 呼叫介面

請將它包裝為既有 authenticated HTTP client 的一個 repository method：

```dart
Future<AiUsageDaily> getMyDailyAiUsage({int recordLimit = 50});
```

以 Dio 為例；`dio` 必須是既有的 Visionnaire session-aware client：

```dart
Future<AiUsageDaily> getMyDailyAiUsage({int recordLimit = 50}) async {
  final safeLimit = recordLimit.clamp(1, 100).toInt();
  final response = await dio.get<Map<String, dynamic>>(
    '/hazard/api/ai_usage/v1/me/usage/daily',
    queryParameters: {'record_limit': safeLimit},
  );
  return AiUsageDaily.fromJson(response.data!);
}
```

Web 不要改成跨 origin 呼叫；native app 則沿用登入 client 的 cookie persistence。
不可自行從 records 加總金額或推算剩餘額度，應直接使用後端的 `budget` 欄位。

## 時間顯示：固定轉成 Asia/Taipei

`records[].started_at` 是帶 offset 的 ISO 8601 instant，後端會正規化為 UTC；例如
`2026-08-28T20:20:47+00:00` 的台北時間是 `2026-08-29 04:20:47`。前端不可直接
格式化原始 `DateTime`，必須轉為 `Asia/Taipei` 後才顯示。

建議使用 `timezone` 與 `intl`，讓使用者即使裝置時區不是台灣，仍看見與每日額度
相同的台北時間：

```dart
import 'package:intl/intl.dart';
import 'package:timezone/data/latest.dart' as tz_data;
import 'package:timezone/timezone.dart' as tz;

// 在 main() 中只執行一次。
void initializeUsageTimeZone() {
  tz_data.initializeTimeZones();
}

String formatUsageTime(String? rawTimestamp) {
  if (rawTimestamp == null) return '—';

  final instant = DateTime.parse(rawTimestamp).toUtc();
  final taipei = tz.TZDateTime.from(instant, tz.getLocation('Asia/Taipei'));
  return DateFormat('yyyy-MM-dd HH:mm:ss').format(taipei);
}
```

`period.started_at` 與 `period.reset_at` 已帶 `+08:00`，但也應走同一個 formatter，
避免不同平台以 UTC 顯示。

## 回應契約

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

欄位說明：

- `budget`：用於頁首進度條與「今日剩餘額度」。`is_exhausted=true` 時提示額度已用完。
- `totals`：目前使用者的今日總計；本地模型也計入 request/token，但通常費用為 `0`。
- `models[]`：每一模型／provider 的今日彙總；`is_local` 用於顯示「本機模型」。
- `records[]`：近期紀錄，依時間新到舊排序；`started_at` 顯示前必須轉為
  `Asia/Taipei`；不含 prompt、completion、API key 或任何身份欄位。
- `record_count`：整個當日的紀錄數；`records.length` 可能較少。
- `records_truncated=true`：只代表近期紀錄依 `record_limit` 被截短，並不影響
  `budget` 或 `totals` 的完整計算。

金額是 USD JSON number，已在後端固定到最多六位小數。UI 只負責顯示，例如
`US$ ${value.toStringAsFixed(4)}`；請不要以浮點運算重新判斷額度是否用盡。

## 錯誤處理與 UX

| HTTP | `detail` | 前端行為 |
| --- | --- | --- |
| 401 | `app_session_expired` | 清除本機 session 狀態並導向既有 Visionnaire 登入流程。 |
| 422 | `{"code":"record_limit_too_large","maximum":100}` | 將 `record_limit` 限制為 100 以下後重試。 |
| 503 | `{"code":"ai_usage_upstream_unavailable"}` | 顯示「暫時無法取得 AI 用量，請稍後再試」與手動重試。 |
| 503 | `{"code":"ai_usage_data_incomplete"}` | 顯示「用量資料尚未完整，暫時不顯示剩餘額度」；不要顯示自行估算的餘額。 |
| 404 | `bff_route_not_allowed` | 部署設定缺少 BFF `ai_usage` allow-list，回報後端，不是使用者操作錯誤。 |

所有非 2xx 回應都可讀取 `X-Request-ID`，在回報問題時提供給後端；不要把完整
response 或使用量資料傳到第三方 crash/analytics 平台。

## 建議畫面與驗收

畫面包含：今日已用／剩餘 USD、重設時間、模型使用明細、本機／雲端次數與近期
紀錄。首次進入與使用者按重新整理時呼叫一次即可；不要高頻輪詢。

驗收條件：

1. 同一帳號在 Web、iOS、Android 顯示同一份資料。
2. 不同 Keycloak 使用者只能看到自己的資料，且 UI 沒有切換他人 ID 的功能。
3. 登出或 session 過期後，API 回 401 並回到登入流程。
4. 每日台灣時間午夜後，新的回應 period 與額度會重設。
