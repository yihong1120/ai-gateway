# Visionnaire AI 用量管理：Flutter 交接規格

本文件對應 `examples/ai_usage` 的管理 API。前端新增「AI 用量管理」頁面；
原「我的 AI 用量」及 `/v1/me/usage/daily` 沿用原契約。

## 公文 PDF：記錄使用者，不扣個人額度

「由服務負擔」與「未歸戶」是不同概念。正式環境的新公文流程已記錄經後端
驗證的登入者 OIDC 身分，涵蓋上傳 PDF／Word／圖片，以及手動／自動 EIP 公文
分析與行事曆模型呼叫；這些請求仍使用公文專用服務 key，不扣個人每日額度。

Flutter 必須把已記名的公文用量放在實際使用者及其目前組別下，顯示 token、
費用及「服務負擔／不扣個人額度」，不能因 billing_scope=service 就移到未歸戶，
也不能將使用者花費顯示為零。此功能使用既有 API：

```text
GET /hazard/api/ai_usage/v1/admin/usage/users?source=official_document&start_date=2026-09-29&end_date=2026-10-28
GET /hazard/api/ai_usage/v1/admin/usage/records?user_id=3&source=official_document&start_date=2026-09-29&end_date=2026-10-28
```

- 使用者列表用 totals 顯示實際用量及成本，service_totals 顯示其中服務負擔金額。
- 明細維持真實 user_id、display_name、group_id、source=official_document、
  billing_scope=service；可依既有日期、使用者及組別篩選。
- budget 只表示個人額度，不能由含公文費用的 totals 扣算。
- OIDC 身分由後端驗證及記錄；Flutter 不傳 subject，也不選擇計費身分。

2026-10-28 再次確認正式註冊的公文 Function 與部署檔一致且啟用，既有正式
記帳驗證紀錄同時保留操作人與空白 end_user，證實可記名但不列入個人額度。
身分隔離、無效身分阻止模型請求及 metadata 傳遞測試均通過。

這裡的 requests 是實際 AI 模型請求數，不是 PDF 份數或功能開啟次數；同一份
公文可能有多次模型請求。命中快取時不新增模型費用，處理工作仍保有 owner。
舊紀錄若當時未存操作人證據，仍列公文共用服務，不能以後來的登入身分倒填。

## 1.5.0：歷史來源重分類與未歸戶分類

2026-10-28 已部署正式 B。38 個測試及 29 項正式 HTTPS／SSO 驗證通過，
9,481 筆修正均已在提交後再次核對。
[部署、分類與逐筆核對收據](ai-usage-reclassification-production-validation-2026-10-28.json)。

歷史公司助理以加密 checkpoint 的模型 response ID，精確連回帳務 request ID，
並核對伺服器 job、計費身分與既有 end_user；共還原 591 筆 company_agent
來源，其中 2026-09-29～2026-10-28 有 95 筆、US$0.27712622。這些紀錄原本
已歸屬正確使用者，重分類後會從一般對話移到公司助理，不會重複計費。

本次另將歷史未歸戶資料寫入分類標記，合計更新 9,481 筆 metadata；保留修改
前備份與證據。使用者、組別、原始費用、token 及個人額度均未變更。
舊公文沒有逐次操作人證據，歸為「公文共用服務」，不指派給任意帳號。

Flutter 在 `summary.unattributed_categories` 顯示各分類的 `category`、`totals`：

| category | 建議標籤 |
|---|---|
| document_service | 公文共用服務（缺少操作人） |
| identity_not_linked | 有身分識別碼，未連結目前帳號 |
| system_operation | 系統／維運請求 |
| failed_request | 失敗請求 |
| missing_identity | 缺少身分證據 |

分類為互斥分組，各分類 totals 加總等於 unattributed_totals。失敗請求保留
原始實際費用；不可自行改成零。分類依專用公文 key、既有身分、call_type 及
status 判定，不以 client 自填分類作為證據。來源與分類是不同的維度。

未歸戶明細每筆新增 `category`，並支援篩選：

```text
GET /hazard/api/ai_usage/v1/admin/usage/unattributed-records?start_date=2026-09-29&end_date=2026-10-28&category=document_service&page=1&page_size=50
```

不傳 category 代表全部；傳無效值回 422。可同時傳 source，但仍不能傳 group_id
或 user_id。回應 category 回顯篩選值，total、unattributed_totals 與 items 為篩選
結果，先篩選再分頁；coverage 保持同日期／來源的完整範圍，不跟 category 縮小。
切換分類時將 page 重設為 1。一般 admin 的權限維持自己 group，不開放未知歸屬。

後端已寫回來源，因此既有前端刷新就會取得修正後的公司助理統計；新增分類卡片、
分類選單與標籤則須由 Flutter 工程依本節串接。

## 1.4.0：完整帳務與其他使用者用量

2026-10-28 已部署正式 B，31 個回歸測試及 25 項公開 HTTPS／SSO 驗證通過。
完整 30 天與全年費用均已與持久帳務對回，也驗證了其他實際有用量的使用者。
[正式部署與對帳收據](ai-usage-ledger-production-validation-2026-10-28.json)。

super admin 在「全部組別、全部使用者」時，summary 的 totals／models／sources
會包含此 tenant 專用 gateway 的完整帳務，包括未歸戶的歷史費用。
日期與 source 篩選仍適用。一般 admin 限自己的 group；指定 group_id 或 user_id
時，只回傳有依據的使用者歸屬，不會將未知費用攤給某個人或某組。

capabilities 新增 `can_view_complete_ledger`。這個權限需要 super admin 且後端
已將專用 gateway 綁定到其 tenant；不可由 Flutter 自行指定 tenant。
summary 新增：

```json
{
  "coverage": {
    "scope": "gateway_ledger",
    "includes_unattributed": true,
    "attributed_record_count": 338,
    "unattributed_record_count": 3956
  },
  "attributed_totals": {"requests": 338, "spend_usd": 0.768230},
  "unattributed_totals": {"requests": 3956, "spend_usd": 20.429681}
}
```

上例 totals 省略其他 token 欄位，完整結構仍與原 UsageTotals 相同。數字為一次
正式對帳樣本，隨新紀錄更新。此樣本的完整總額為 US$21.197911、公文來源費用
為 US$20.406168，不再只回傳已歸戶的 US$0.768230。

有兩種不同的分拆維度，不要重複加總：

- 歸屬：attributed_totals + unattributed_totals = totals。
- 計費：user_totals + service_totals = totals。未歸戶也可能有上游 end_user，
  所以未歸戶費用不一定全是 service_totals。

coverage.scope=`attributed_users` 表示目前只看某人／某組，或呼叫者沒有完整帳務
權限。includes_unattributed=false 時不要顯示「未歸戶費用為零」，應顯示「此範圍
僅列已歸戶用量」。已知其他 tenant 的帳號紀錄會被排除，不能冒充未歸戶而跨租戶。

Flutter 畫面修改：

1. gateway_ledger 模式顯示「區間總帳」，直接使用 totals；另加「已歸戶」與
   「未歸戶」卡片。其他模式標示「使用者／組別用量」。
2. 使用者列表繼續顯示真實的使用者、組別、用量與花費；點選某人時三支既有
   API 都傳 user_id，必要時再傳相同日期與來源。不要替未歸戶事件建立假帳號。
3. 新增「未歸戶明細」分頁，只在 coverage.includes_unattributed=true 時顯示。
   日期及 source 與摘要一致；不要傳 group_id 或 user_id。
4. 使用者列表加總對應 attributed_totals；再加 unattributed_totals 才是完整總帳。
   使用者人數是目前目錄人數，不包含無法確認身分的歷史操作人。

未歸戶明細 API：

```text
GET /hazard/api/ai_usage/v1/admin/usage/unattributed-records?start_date=2026-09-29&end_date=2026-10-28&source=official_document&page=1&page_size=50
```

回應有 period、selection、page、page_size、total、has_more、coverage、
unattributed_totals、items。items 是原計量欄位（時間、模型、provider、source、
billing_scope、token、spend_usd），另加：

- accounting_id：24 字元不透明參照，可作為列表 key。
- attribution_reason：missing_identity 顯示「缺少操作人」，identity_not_linked
  顯示「身分未連結」。

此明細沒有 user_id、username、group_id、Keycloak sub、API key 或原始 metadata。
一般 admin、未配置完整帳務權限，或同時指定 group_id／user_id 時回傳
403 ai_usage_unattributed_forbidden。

原 `/records` 維持有真實 user_id 的使用者明細，total 仍為已歸戶筆數，避免
舊版 Flutter 的非 nullable 使用者模型解碼失敗；完整筆數為兩份明細之和。
`/users`、`/records` 與新明細回應均新增 coverage、unattributed_totals。

1.5.0 已用精確 response ID 還原有證據的舊 company_agent 來源；其餘缺乏來源
證據的紀錄保留原分類，費用仍列入可見完整帳務。帳務筆數包含失敗及維運紀錄，不全是
成功的模型呼叫。未知歸屬的歷史公文費用會列出，仍不會憑空指派操作人。

## 1.3.0 對帳時的涵蓋限制（1.4.0 已新增完整帳務視圖）

2026-10-28 已以真實 BFF session 與正式持久帳務核對
2026-09-29～2026-10-28，結果見
[30 日對帳紀錄](ai-usage-30day-reconciliation-2026-10-28.json)。
API 回傳 338 筆、US$0.768230，與可歸戶的帳務一致；但 gateway 同期另有
2,582 筆沒有操作人身分的舊公文，US$20.406161，及其他無法對應目前帳號的
帳務。全部 gateway 帳務合計 US$21.197911，包含維運、驗證及失敗紀錄，
不能將這個 gateway 總額直接當成任何使用者／組別的費用。

1.3.0 的 `totals`、`models`、`sources`、`users`、`records` 都只統計可歸戶資料。
頁首建議標示「已歸戶區間統計」，不要標成完整團隊帳務總額；13 個已連結身分
及 8 個未連結帳號也不代表涵蓋了所有歷史操作人的用量。

來源列表只涵蓋有歸戶的紀錄：公司工作助理 1 筆／19 tokens、公文 1 筆／
17 tokens 是啟用來源記帳時的兩筆驗證請求，不是這兩項服務整個月的全部使用。
缺少來源標記的舊 company_agent 流量，可能與一般 AI 對話合併，不能可靠拆分。
不要以時間相近、檔名、email 或聊天內容猜測使用者／組別歸屬。

若要顯示未歸戶服務用量，須另定有服務／tenant 授權邊界的獨立統計欄位，
1.3.0 未提供；1.4.0 已依上節規格新增。前端仍不能自行把 gateway 總額併入
目前 group 的 totals。
金額大於 0 但不足 US$0.01 時可顯示「< US$0.01」或六位小數，避免誤看為無費用。

## 1.3.0：日期範圍選擇

2026-10-28 已部署正式 B。27 個回歸測試與 20 項正式 HTTPS／SSO 驗證通過；
全年查詢的請求數、token 及費用已與持久帳務核對一致。詳見
[日期範圍部署驗證](ai-usage-range-production-validation-2026-10-28.json)。

管理頁新增「今天、近 7 天、近 30 天、本月、自訂日期」選項，預設今天。
summary、users、records 三支 API 同時新增 `start_date`、`end_date`；日期格式
為 `YYYY-MM-DD`，依 `Asia/Taipei` 解讀，**包含起日與迄日的完整日期**。
兩個參數必須一起傳；一起省略就查今天。來源、組別與使用者篩選可併用。

```text
GET /hazard/api/ai_usage/v1/admin/usage/summary?start_date=2026-10-01&end_date=2026-10-28
GET /hazard/api/ai_usage/v1/admin/usage/users?start_date=2026-10-01&end_date=2026-10-28&group_id=10&page=1&page_size=50
GET /hazard/api/ai_usage/v1/admin/usage/records?start_date=2026-10-01&end_date=2026-10-28&source=official_document&user_id=3&page=1&page_size=50
```

每次最多 366 天（含首尾日），實際上限讀取 capabilities.max_range_days。
這是單次區間長度限制，不代表只能查最近一年。不能選未來日期、迄日早於起日
或只傳一個日期。歷史資料僅包含上游仍留存且能歸戶的紀錄；舊公文的身分限制
仍如 1.2.0 說明，不會因開放日期篩選而補出不存在的歸屬資料。

capabilities 新增：

```json
{
  "supports_date_range": true,
  "max_range_days": 366,
  "default_period": "today"
}
```

period_mode 保留原值 today，維持舊版前端解碼相容；新前端使用
supports_date_range 決定是否提供日期選擇。所有資料回應的 period 新增起訖日期
及 is_today，並保留舊欄位：

```json
{
  "date": "2026-10-01",
  "start_date": "2026-10-01",
  "end_date": "2026-10-28",
  "is_today": false,
  "timezone": "Asia/Taipei",
  "started_at": "2026-10-01T00:00:00+08:00",
  "reset_at": "2026-10-29T00:00:00+08:00"
}
```

date 是相容欄位，值為起日；區間標題改用 start_date／end_date。
reset_at 是迄日翌日午夜的「不包含」上界，不代表歷史帳務會再次重設。
後端保留迄日 23:59:59.999999 的紀錄，排除翌日 00:00:00。

**只有 period.is_today=true 才提供使用者的每日 budget。** 歷史單日或多日
查詢的 users.items[].budget 為 null，即使範圍包含今天也一樣；前端隱藏每日
剩餘額度／進度條，改顯示區間用量，不把多日費用套用單日預算。
未連結身分仍依 ai_identity_linked 顯示；不能單憑 budget=null 判斷未連結。
原個人 API 仍提供今天及每日額度。

Flutter 日期選擇規則：

1. 以台北日期計算今天、近 7／30 天及本月，不受裝置所在時區影響。
   近 7 天為「今天減 6 天」到今天；近 30 天為「今天減 29 天」到今天。
2. 可使用 showDateRangePicker；選取後以年月日組成 YYYY-MM-DD，不先轉 UTC
   再截字串，避免選取日期偏移。
3. summary／users／records 一律傳相同日期與其他篩選；換日期後將分頁歸 1，
   取消舊請求或忽略過期回應。組別與使用者選擇可保留。
4. 日期錯誤先在 UI 提示，後端仍會驗證；不要把 422 或 503 顯示成零用量。
5. 只選「今天」快捷項目時可在跨午夜重算日期；自訂區間保留使用者所選日期。

新增的 422 detail.code：ai_usage_date_range_required（須成對）、
ai_usage_invalid_date_range（起迄顛倒）、ai_usage_future_date_not_allowed（未來日期）、
ai_usage_date_range_too_long（超過 max_range_days）。日期格式錯誤仍使用 FastAPI
標準 422 detail 陣列。其他角色、tenant、group 權限與來源計費規則維持原契約。

較長區間遇到 LiteLLM 單次一萬筆限制時，後端會自動分段查詢，首尾邊界不重複
計數；整體查詢仍受頁數上限保護，無法取得完整資料時回傳
503 ai_usage_data_incomplete，不能顯示部分總計。

## 1.2.0：公文 PDF 與 company_agent 用量

2026-10-27 已部署至正式 B。77 項 pytest、14 項 unittest 與 14 項公開
HTTPS／真實 SSO 驗證通過；兩筆最小模型請求已確認來源 metadata 持久記帳及
公文不扣個人額度。詳見 [來源記帳部署紀錄](ai-usage-sources-production-validation-2026-10-27.json)。

管理統計新增公文服務及 company_agent 的來源分類，以下欄位為相容性新增。
在 summary、users、records 呼叫時可多傳 `source`，省略代表全部來源。
三支資料 API 必須使用相同篩選；切換來源時重新讀取摘要及分頁資料。

| source 值 | 建議顯示名稱 |
|---|---|
| openwebui | 一般 AI 對話 |
| official_document | 公文 PDF／Word／圖片 |
| company_agent | 公司工作助理 |
| company_ai | Company AI 工具 |
| unknown | 其他／未分類 |

例如：

```text
GET /hazard/api/ai_usage/v1/admin/usage/summary?group_id=10&source=official_document
GET /hazard/api/ai_usage/v1/admin/usage/users?group_id=10&source=company_agent&page=1&page_size=50
GET /hazard/api/ai_usage/v1/admin/usage/records?user_id=3&source=official_document&page=1&page_size=50
```

summary 與 users.items[] 都新增以下欄位：

- `user_totals`：此查詢範圍內計入個人用量的統計，結構同 totals。
- `service_totals`：此查詢範圍內由服務負擔的統計，結構同 totals。
- `sources[]`：`{"source":"official_document","totals":{...}}`，分來源完整統計。

`totals` 現在包含個人及服務用量。公文費用會列入 totals／service_totals，
但維持不扣個人每日額度。company_agent 維持個人計費，列入 user_totals。
**僅查今日時，使用者的 budget 是本人整個今日的個人額度，與 source 篩選無關**；
1.3.0 的歷史或多日查詢 budget=null。
不要用包含公文費用的 totals.spend_usd 計算剩餘額度。

records.items[] 新增 `source` 與 `billing_scope`。billing_scope 為 user 或 service，
建議明細增加「來源」及「個人計費／服務負擔」兩欄。個人 API 的 records 也會
相容性新增這兩個欄位；既有 budget 計算維持原值。

selection 新增 `source`（可為 null）；其餘 ID 與權限規則相同。
sources[] 僅列出有紀錄的來源；來源選單使用上表的固定完整清單，不能只依當日
回應建立，否則零用量來源無法選取。users 仍列出範圍內目前成員，沒有該來源
用量者顯示 0，未連結身分者維持原顯示方式。

公文處理會以經後端確認的操作人身分歸戶，支援上傳與 EIP 公文／行事曆模型
呼叫。來源只計算經 LiteLLM 的模型請求與費用；本地 PDF 轉換、OCR、排版及
檔案輸出不會憑空換算成 token 或費用。

舊公文紀錄沒有保存操作人，因此無法可靠回填到使用者／組別；只會歸戶啟用
新記帳後有身分標記的紀錄。company_agent 舊紀錄原已計入個人總量，但沒有
來源標記；1.5.0 已透過精確 response ID 還原其中有證據的來源，詳見前節。

Flutter repository 的 summary／users／records 請各增加 `String? source` 參數，
並在 queryParameters 加上 `if (source != null) 'source': source`。
不需要另開 API、變更登入或重新取得金鑰。

## 可直接交給 Flutter 工程師的需求

> 請新增「AI 用量管理」頁面，依本文件串接後端 API。
> 使用既有帶 BFF session cookie 的 authenticated Dio client，所有請求走
> `/hazard/api/ai_usage/v1/admin/usage/`。
> 先取得 capabilities，只有 can_view_admin_usage=true 才顯示管理入口。
> super admin 可選全部組別或指定組別；admin 固定自己的 group。
> 頁面提供所選日期區間的總計、模型統計、使用者用量表，以及可分頁的使用明細。
> 點選使用者後依本地 user_id 查詢個人摘要與明細。
> 提供今天、近 7／30 天、本月與自訂日期；組別按目前成員歸屬。
> 總計及個人額度使用後端欄位，不從分頁 items 重新計算。
> 完整帳務模式另顯示已歸戶／未歸戶卡片與未歸戶明細，兩者加總才是總帳。
> API 的部署前提、參數、JSON、錯誤及驗收條件均在本文件。

## 部署前提與登入

2026-10-28 正式 B 已更新至 1.3.0，沿用專用唯讀帳號，Flutter 可開始串接
日期範圍、來源及管理路由。27 個回歸與 20 項公開 HTTPS／真實 SSO 驗證通過，
詳見 [正式部署驗證紀錄](ai-usage-range-production-validation-2026-10-28.json)。

其他環境仍須設定唯讀的 `AI_USAGE_ADMIN_DATABASE_URL` 並重啟 AI Usage 服務，
才能呼叫管理功能。
設定方法見 [後端 README](../examples/ai_usage/README.md#管理用量端點與部署)。
未設定會回傳 `503 ai_usage_admin_not_configured`，不能當成「沒有用量」。

所有平台沿用既有 BFF session 登入與同一個保存 cookie 的 HTTP client。
Web 使用同源相對路徑；native 使用已登入的 server origin 與 cookie jar。
GET 不需要 CSRF header；不要另附 Authorization，也不要連 8012 loopback。
不需要任何 LiteLLM key、Keycloak sub 或新增登入流程。

本文件所有路徑以下列 prefix 開頭：

```text
/hazard/api/ai_usage/v1/admin/usage
```

## 權限與 capabilities

```http
GET /hazard/api/ai_usage/v1/admin/usage/capabilities
```

super admin 範例：

```json
{
  "can_view_admin_usage": true,
  "scope": "all_groups",
  "managed_group_id": null,
  "can_filter_groups": true,
  "timezone": "Asia/Taipei",
  "period_mode": "today",
  "supports_date_range": true,
  "max_range_days": 366,
  "default_period": "today",
  "group_attribution": "current_membership"
}
```

| 身分 | scope | can_view_admin_usage | managed_group_id | can_filter_groups |
|---|---|---|---|---|
| super admin | all_groups | true | null | true |
| admin 且有 group | own_group | true | 自己 group ID | false |
| 一般使用者／未分組 admin | self | false | null | false |

前端以這個回應決定入口及選單，不自行以 username 或 role 推算權限。
後端每次都會重新檢查權限；一般使用者直接進管理網址仍應擋回個人頁。
capabilities 無法取得時，保留個人頁並顯示管理功能暫時無法使用，不能顯示假總計。

「所有組別」限呼叫者的 tenant，包含尚未分組的使用者。現行 super admin
認定沿用既有後端政策，不由 Flutter 決定。

## 組別、篩選及分頁

```http
GET /hazard/api/ai_usage/v1/admin/usage/groups
```

```json
{
  "items": [
    {"group_id": 10, "group_name": "甲組"},
    {"group_id": 20, "group_name": "乙組"}
  ]
}
```

super admin 的選單最前面自行加「全部組別」（不傳 group_id），其餘使用上述
items。只列同 tenant 目前有成員的組別；admin 只會拿到自己的 group。
未分組使用者包含於全部組別，第一版沒有獨立的「未分組」篩選值。

| 參數 | 適用端點 | 規則 |
|---|---|---|
| group_id | summary、users、records | 可省略，正整數；super admin 省略為全部，admin 省略為自己的 group |
| user_id | summary、users、records | 可省略，正整數；使用 users.items[].user_id，本地帳號 ID |
| source | summary、users、records | 可省略；openwebui、official_document、company_agent、company_ai 或 unknown |
| start_date | summary、users、records | YYYY-MM-DD，含起日；與 end_date 一起省略代表今天 |
| end_date | summary、users、records | YYYY-MM-DD，含迄日；須與 start_date 一起提供 |
| page | users、records | 預設 1，整數且 ≥1 |
| page_size | users、records | 預設 50，整數 1–100 |

同時傳 group_id 和 user_id 時，使用者必須屬於該組及授權範圍。
選取單一使用者時三個資料端點都帶相同篩選，確保卡片、表格和明細一致。
沒有模型、email、username、end_user 或 record_limit 篩選。省略條件時，
直接省略 query parameter，不要傳送字串 "null" 或空字串。

users 依 user_id 升冪；records 依時間新到舊，同時間依 user_id 及計量欄位排序。
回應 total 是完整符合範圍的筆數，不是當頁 items.length。page 超出最後一頁時
回傳空 items、has_more=false。這是即時分頁，沒有固定快照；新紀錄可能推動頁面，
刷新時回到第一頁。不同端點的總計也可能因即時入帳而略有時間差。

## 區間摘要（省略日期為今天）

```http
GET /hazard/api/ai_usage/v1/admin/usage/summary?group_id=10
```

```json
{
  "period": {
    "date": "2026-10-27",
    "timezone": "Asia/Taipei",
    "started_at": "2026-10-27T00:00:00+08:00",
    "reset_at": "2026-10-28T00:00:00+08:00"
  },
  "selection": {
    "scope": "all_groups",
    "group_id": 10,
    "user_id": null,
    "group_attribution": "current_membership"
  },
  "user_count": 5,
  "linked_user_count": 4,
  "unlinked_user_count": 1,
  "totals": {
    "requests": 3,
    "cloud_requests": 3,
    "local_requests": 0,
    "prompt_tokens": 300,
    "completion_tokens": 60,
    "cache_read_input_tokens": 0,
    "total_tokens": 360,
    "spend_usd": 1.75
  },
  "models": [
    {
      "model": "cloud-model",
      "provider": "unknown",
      "is_local": false,
      "requests": 3,
      "prompt_tokens": 300,
      "completion_tokens": 60,
      "cache_read_input_tokens": 0,
      "total_tokens": 360,
      "spend_usd": 1.75
    }
  ]
}
```

selection.scope 表示操作者權限範圍；即使 super admin 指定一組，仍為
all_groups。selection.group_id／user_id 才是本次實際篩選；admin 未傳 group_id
時，回應會填自己的 group ID。

UI 卡片顯示花費 USD、請求數、token、範圍內人數；models 顯示各模型統計。
沒有組別預算／組別剩餘額度，不要用人數乘上每日額度。

## 使用者用量表

```http
GET /hazard/api/ai_usage/v1/admin/usage/users?group_id=10&page=1&page_size=50
```

回應含 period、selection（結構同 summary），以及：

```json
{
  "page": 1,
  "page_size": 50,
  "total": 5,
  "has_more": false,
  "items": [
    {
      "user_id": 3,
      "username": "alice",
      "display_name": "王小明",
      "group_id": 10,
      "group_name": "甲組",
      "account_status": "active",
      "ai_identity_linked": true,
      "budget": {
        "limit_usd": 4.0,
        "used_usd": 1.0,
        "remaining_usd": 3.0,
        "is_exhausted": false
      },
      "totals": {
        "requests": 1,
        "cloud_requests": 1,
        "local_requests": 0,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "cache_read_input_tokens": 0,
        "total_tokens": 120,
        "spend_usd": 1.0
      }
    }
  ]
}
```

上例僅展示一個 item 的結構，實際 items 包含當頁全部項目。
建議欄位：姓名／帳號、組別、請求數、token、花費、個人剩餘額度及查看明細按鈕。
點選列取得 user_id，再讀 summary 與 records；選單要涵蓋所有使用者時，需讀完
users 分頁，不能只以第一頁當成完整名單。大組別可直接在分頁表格中選取。

| 狀況 | 顯示方式 |
|---|---|
| ai_identity_linked=true 且 requests=0 | 所選區間尚無用量；僅今日查詢顯示每日 budget |
| ai_identity_linked=false | 尚未連結 AI 身分；budget=null，剩餘額度顯示「—」 |
| group_id／group_name=null | 未分組 |
| period.is_today=false | 隱藏每日 budget；budget=null 不代表身分未連結 |
| account_status 非 active | 顯示帳號狀態；已有用量仍保留 |

account_status 可能為 active、email_unverified、pending_admin_approval、rejected、
suspended；未知狀態以一般文字顯示。顯示名稱優先用 display_name。
budget 的規則與既有個人頁完全相同，前端不重算金額或額度。

## 使用明細

```http
GET /hazard/api/ai_usage/v1/admin/usage/records?group_id=10&user_id=3&page=1&page_size=50
```

回應同樣含 period、selection，以及：

```json
{
  "page": 1,
  "page_size": 50,
  "total": 1,
  "has_more": false,
  "items": [
    {
      "user_id": 3,
      "username": "alice",
      "display_name": "王小明",
      "group_id": 10,
      "group_name": "甲組",
      "started_at": "2026-10-26T19:00:00+00:00",
      "model": "cloud-model",
      "provider": "unknown",
      "is_local": false,
      "prompt_tokens": 100,
      "completion_tokens": 20,
      "cache_read_input_tokens": 0,
      "total_tokens": 120,
      "spend_usd": 1.0
    }
  ]
}
```

started_at 可為 null，顯示「—」；有值時固定轉成 Asia/Taipei。
上述時間顯示為 2026-10-27 03:00:00。沿用個人頁的 timezone formatter。
明細只包含計量與顯示身分，不包含聊天內容、prompt、completion 文字或憑證。
budget 與費用數字用 num.toDouble() 解析，不假設 JSON 一定是 double。

## Flutter repository 呼叫範例

下列 dio 必須是已登入、可攜帶 BFF cookie 的既有 client。示例回傳 Map，
正式工程請依上述契約建立型別及 fromJson，nullable 欄位不可強制解包。

```dart
class AdminAiUsageRepository {
  AdminAiUsageRepository(this.dio);
  final Dio dio;
  static const base = '/hazard/api/ai_usage/v1/admin/usage';

  Future<Map<String, dynamic>> _get(
    String endpoint, {
    Map<String, dynamic>? query,
    CancelToken? cancelToken,
  }) async {
    final response = await dio.get<Map<String, dynamic>>(
      '$base/$endpoint',
      queryParameters: query,
      cancelToken: cancelToken,
    );
    return response.data!;
  }

  Future<Map<String, dynamic>> capabilities() => _get('capabilities');
  Future<Map<String, dynamic>> groups() => _get('groups');

  Map<String, dynamic> _filters(
    int? groupId, int? userId, String? source,
    String? startDate, String? endDate,
  ) => {
    if (groupId != null) 'group_id': groupId,
    if (userId != null) 'user_id': userId,
    if (source != null) 'source': source,
    if (startDate != null) 'start_date': startDate,
    if (endDate != null) 'end_date': endDate,
  };

  Future<Map<String, dynamic>> summary({
    int? groupId, int? userId, String? source, CancelToken? cancelToken,
    String? startDate, String? endDate,
  }) => _get('summary', query: _filters(groupId, userId, source, startDate, endDate),
    cancelToken: cancelToken);

  Future<Map<String, dynamic>> users({
    int? groupId, int? userId, String? source, int page = 1, int pageSize = 50,
    String? startDate, String? endDate,
    CancelToken? cancelToken,
  }) => _get('users', query: {
    ..._filters(groupId, userId, source, startDate, endDate), 'page': page, 'page_size': pageSize,
  }, cancelToken: cancelToken);

  Future<Map<String, dynamic>> records({
    int? groupId, int? userId, String? source, int page = 1, int pageSize = 50,
    String? startDate, String? endDate,
    CancelToken? cancelToken,
  }) => _get('records', query: {
    ..._filters(groupId, userId, source, startDate, endDate), 'page': page, 'page_size': pageSize,
  }, cancelToken: cancelToken);

  Future<Map<String, dynamic>> unattributedRecords({
    String? source, String? category, String? startDate, String? endDate,
    int page = 1, int pageSize = 50, CancelToken? cancelToken,
  }) => _get('unattributed-records', query: {
    ..._filters(null, null, source, startDate, endDate),
    if (category != null) 'category': category,
    'page': page, 'page_size': pageSize,
  }, cancelToken: cancelToken);
}
```

需要 `package:dio/dio.dart`。Web 以同源相對路徑呼叫；native 的 Dio baseUrl
必須包含已登入的 server origin。

畫面載入順序：capabilities → groups → summary 與 users；明細分頁可按需載入。
切換組別時清除 userId、重設頁碼並重新取得資料。快速切換時取消前次請求，或用
request generation ID 忽略舊回應，避免舊組資料覆蓋目前組別。
「今天」快捷項目可在跨午夜重載；自訂日期保留所選區間，回到頁面時刷新。

## 錯誤與資料處理

錯誤格式：`{"detail":{"code":"ai_usage_scope_forbidden"}}`。
422 使用 FastAPI 標準 detail 陣列，不保證有 code。

| HTTP／code | UI 行為 |
|---|---|
| 401 missing_access_token／invalid_access_token | 沿用既有登入過期處理 |
| 403 ai_usage_admin_required | 清除管理資料並離開管理頁／重讀 capabilities |
| 403 ai_usage_account_unavailable | 帳號未連結、停用或 tenant 不可用；顯示無法使用管理功能 |
| 403 ai_usage_scope_forbidden | 清除失效篩選，重新載入可見組別 |
| 404 ai_usage_selection_not_found | 使用者／組別不存在或已不在可見範圍；清除篩選後刷新 |
| 422 | 檢查前端傳送的 ID、page、page_size；不是零用量 |
| 503 ai_usage_admin_not_configured | 管理功能尚未啟用，請後端設定資料庫連線 |
| 503 ai_usage_directory_unavailable | 帳號目錄暫時無法取得，提供重試 |
| 503 ai_usage_upstream_unavailable | 用量服務暫時無法取得，提供重試 |
| 503 ai_usage_data_incomplete | 無法取得完整統計；不要呈現部分總計或估算剩餘額度 |
| 其他 5xx／BFF 錯誤 | 沿用既有服務錯誤處理及重試 |

管理資料及錯誤回應均帶 no-store；不寫入 SharedPreferences、SQLite、持久快取、
analytics 或 crash report。登出、權限改變或收到 403 後清除記憶體中的管理資料。

資料範圍按目前帳號目錄及成員歸屬：轉組後歷史紀錄跟隨目前組別；尚未刪除的
停用帳號仍可列出。已刪除帳號／identity、無身分歸屬的流量不會納入，不能把此頁
稱為涵蓋所有歷史身分的帳務稽核報表。

## 前後端共同驗收

1. 一般使用者保留原個人頁，沒有管理入口；直接呼叫管理資料 API 回 403。
2. admin 只能看自己 group；手動修改 group_id 或 user_id 仍無法跨組。
3. super admin 可切換全部／指定組別與使用者，且仍受 tenant 邊界限制。
4. 切換 group 會清除舊 user 與頁碼；晚到的舊回應不能覆蓋目前選擇。
5. 使用者表能區分零用量與未連結身分；未分組、無時間及無 budget 都能顯示。
6. records 換頁不改變完整範圍的 summary；不從當頁 items 推算總額。
7. 指定使用者、僅查今日時，個人計費及 budget 與原個人 API 一致；管理總計
   另含服務費，完整帳務模式還含未歸戶費用。時間統一台北時間。
8. 角色、組別或帳號狀態改變後，重新查詢即遵守新的權限範圍。
9. 503、權限錯誤和空資料有不同畫面，不把失敗顯示成 0。
