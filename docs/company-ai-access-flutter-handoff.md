# Flutter changdar-ai 自助設定串接

## 2026-10-24：Codex 預設模型修正（1.2.3）

下載／Policy 契約維持不變。新版 `codex` 入口明確選擇公司管理設定，並將公司模型、provider、模型清單與 daemon 設定套用為啟動預設，避免使用者家目錄的個人 `.codex/config.toml` 覆蓋預設模型。
初始預設仍為 `gemini-3.8-flash`；`/model` 可選三個公司模型，`-m` 可明確指定。
既有裝置可下載新版 PS1，以 `-Tools Codex -RefreshModelsOnly` 更新啟動器與模型設定，不需重新綁定；此操作須在一般員工 PowerShell 執行。
Flutter 無須新增模型參數或改變裝置核准流程。
本機 WIN11 員工 VM 已使用正式新版 PS1 更新既有綁定，直接 `codex` 顯示 Gemini 3.8 Flash，`/model` 僅列三個公司模型，`-m gemini-3.5-flash-lite` 可正常指定。

## 2026-10-24：EXE 1.2.1 錯誤恢復更新

正式下載仍使用相同 `windows_installer_url`，這次不需要修改 Flutter API 或下載按鈕。
請重新下載 EXE，避免繼續開啟 Downloads 中舊的 1.2.0 檔案。
新版加入階段提示、驗證碼倒數、開啟／複製核准連結、安全取消與重新設定，
暫時性下載／輪詢失敗最多重試兩次；權限或政策拒絕不會自動重試。
核准後有獨立且有限的啟用期限，避免接近十分鐘才核准造成立即失敗。
完成後須完整關閉所有 Windows Terminal／PowerShell 視窗，再重新開啟以載入新環境設定。
員工仍須親自核准；不增加自動核准或 Flutter 領取金鑰的流程。

## 2026-10-24：Windows EXE 下載與品牌更新（正式版 1.2.0）

這一節是目前正式環境的整合方式；下方 2026-10-18 等紀錄保留為歷史。
前端 repo 在另一台電腦，本次只實作及部署後端、Windows EXE 與 B 內建下載頁。
Flutter 工程師需要更新實際下載按鈕及顯示名稱。

1. 頁面／選單顯示名稱統一為 `changdar-ai`。API 路徑與既有綁定資料仍沿用，無須搬移。
2. 沿用正式網站的 BFF session，GET 相對路徑 `/bff/ai_access/api/company-ai/policy`。
3. 從回應讀取新增的 `product_name`、`windows_installer_url`。按鈕顯示「下載 changdar-ai Windows 設定程式」。
4. 使用瀏覽器下載 `windows_installer_url`，以伺服器 Content-Disposition 檔名儲存。
   不要把 EXE 存成 `.ps1`，不要當成文字顯示或在瀏覽器執行，不需向下載 URL 附加金鑰／Token。
5. 使用者下載後直接雙擊 EXE，以一般員工帳號開啟，在視窗中選擇 Claude Code／Codex（預設兩者），
   同意目前帳號的公司簽署憑證信任，再按「開始設定」。已安裝工具會略過下載，Codex 保留核准版本檢查。
6. EXE 顯示安裝／核准進度並開啟公司綁定頁，使用者親自核對驗證碼後核准。
   Flutter 不自動核准、不收取裝置金鑰，也不需新增員工／預算／模型指定參數。

Policy 節錄（其他欄位保留）：

```json
{
  "product_name": "changdar-ai",
  "windows_installer_url": "https://changdar-server.mooo.com/company-ai/downloads/changdar-ai-setup.exe",
  "windows_setup_url": "https://changdar-server.mooo.com/company-ai/downloads/Setup-CompanyAI.ps1",
  "codex_enabled": true,
  "codex_release": "0.164.0"
}
```

`windows_setup_url` 仍代表 PS1，僅供既有版本／IT 使用；主下載按鈕請改用 `windows_installer_url`。
舊後端缺少新增欄位時，請顯示「尚未提供 Windows EXE，請聯絡管理員」，避免將 PS1 當成 EXE。
可在資料模型把 `product_name` 預設為 `changdar-ai`、`windows_installer_url` 設為 nullable。
更新後的契約見 [OpenAPI](company-ai-access-openapi.json)。

下載契約：

| 項目 | 值 |
| --- | --- |
| 方法 | GET，瀏覽器正常下載／外部開啟 URL |
| 路徑 | `/company-ai/downloads/changdar-ai-setup.exe` |
| 檔名 | `changdar-ai-setup.exe` |
| Content-Type | `application/octet-stream` |
| Content-Disposition | `attachment; filename="changdar-ai-setup.exe"` |
| Cache-Control | `no-store` |
| 網路 | 公司 LAN／已核准 VPN；來源限制與既有 PS1 下載相同 |

Flutter 顯示文字可使用：

> 下載後雙擊開啟。預設安裝 Claude Code 與 Codex，已安裝的工具會略過下載。
> 請以一般員工帳號使用，並在瀏覽器核對驗證碼後核准。

若使用目前專案既有的 URL 開啟工具，將按鈕 callback 的 URI 改為 policy 回傳的 installer URL；
只接受與 `portal_origin` 相同 HTTPS origin、上述固定下載路徑的 URL。
不需要在 Flutter 導入桌面 PowerShell 操作、憑證匯入功能或 API Key 儲存。

錯誤處理：policy 401 沿用既有 SSO；下載 403 提示連接公司網路／VPN 並確認內部 DNS；
404 表示尚未部署該檔案或後端版本不同。不要把 HTML／JSON 錯誤內容另存為 EXE。
瀏覽器直接下載時讓瀏覽器呈現下載失敗；若使用 HTTP client 下載，必須先確認狀態碼為 200。

內部 CA 簽署的 EXE 首次執行仍可能出現 Windows／SmartScreen 提示，簽章不等於公開信譽。
EXE 僅在使用者明確同意後，為目前 Windows 帳號加入固定指紋的公開根／發行者憑證，
並驗證完整簽章與撤銷狀態。它不更動使用者／電腦的持續性 PowerShell 執行政策，
公司 GPO 仍優先；設定子行程使用 RemoteSigned，已簽署的公司憑證 helper 使用 AllSigned。
既有個人供應商設定預設保留，使用者必須勾選切換選項才會替換。

驗收：按鈕下載到 EXE 正確檔名、一般帳號能開啟、有明確信任與切換選項、預設兩工具、
可看到安裝進度與驗證碼、瀏覽器必須明確核准、完成後新終端可使用 `claude`／`codex`。
Windows x64 已在簽署 VM 驗證；ARM64 使用同一個 AnyCPU EXE，需另外做實機安裝驗收。

這份交接供另一台電腦的 Flutter 工程師實作「AI 工具 → Claude Code」頁面。後端與 Windows 設定工具在 A 的 `ai-gateway` repo；Flutter 原始碼仍在你們自己的 repo。先串接 `https://ai-dev.changdar-server.mooo.com`，驗收後再部署正式公司網站。LiteLLM Enterprise 不在此設計中。

## 2026-10-18：公司 dev 網站的 BFF 已接通

若整合進既有 `https://dev.changdar-server.mooo.com` Flutter app，使用相對 API 前綴
`/bff/ai_access/api/company-ai`，沿用該網站的 BFF session。寫入請求同時需要既有
`X-CSRF-Token` 與 `X-Company-AI-Action: 1`。不要在這個網站使用 `/api/company-ai`，
也不要跨源讀取 ai-dev 的 session。

實際 BFF 位於 `Construction-Hazard-Detection` repo，在 A 的 `127.0.0.1:19005` 運行，
已配置固定 upstream `http://127.0.0.1:18083`。company-ai 驗證相同 Keycloak issuer 與
dev 專用 `visionnaire-dev-api` audience，另明確允許 dev 網站的寫入 Origin。
AI 使用權限仍需 `open-webui-dev` 的 `dev-user` 或 `dev-admin`，不會因登入公司 dev 就自動授權。

已透過公開 HTTPS 與真實 OIDC/PKCE callback 驗證 policy、本人清單、核准、重複核准拒絕、
撤銷及登出，並驗證 CSRF、Origin/action 與不同 audience 的拒絕。測試電腦已撤銷，測試 SSO session 已清理。

Windows 工具與模型入口仍使用 policy 回傳的 ai-dev 網址，核准連結目前仍開啟 ai-dev 內建頁。
本次沒有把 Windows 的入口切換到公司 dev，也沒有部署正式環境。

若整個 Flutter app 部署在 dev 網站根目錄，請使用 `flutter build web --base-href /`；
只有 build 的全部檔案部署在 `/company-ai/portal/` 子目錄時，才使用本文後面的子路徑 build 指令。
目前 dev 根目錄的 build 使用子路徑 base，會導致 bootstrap/manifest 載入失敗；這是前端 artifact 的部署問題。

## 先確認開發環境

| 項目 | ai-dev |
| --- | --- |
| HTTPS 入口 | B 的既有 Nginx，轉送至 A |
| 頁面與 API | A 的獨立 dev Open WebUI，loopback 18082 |
| 金鑰佈建服務 | A 的獨立行程，loopback 18083 |
| LiteLLM | A 的 dev 14002 與 dev DB |
| 登入 | `open-webui-dev` client；必須有 `dev-user` 或 `dev-admin` |
| Flutter 頁面預留位置 | `/company-ai/portal/` |
| 可用的預設核准頁 | `/company-ai/setup` |
| API 前綴 | `/api/company-ai` |
| 模型 API 前綴 | `/company-ai/v1`，Flutter 不呼叫此處來領取金鑰 |

目前只有既有 dev 測試帳號具有 dev 角色。工程師若無法登入，請由公司 Keycloak 管理員授予該帳號 **open-webui-dev** 的 `dev-user`；不要修改正式 client 的角色或使用共用測試帳密。

**ai-dev 的登入是 Open WebUI session，正式公司網站則是 BFF session。兩者的 cookie 不共用。** 在 ai-dev 網站先完成登入，再回到核准網址。不要把正式公司的 access token、BFF cookie 或 LiteLLM key 複製到 ai-dev。

## Flutter 要做的畫面

### 一般設定頁

呼叫 `GET /policy` 與 `GET /devices`。提供 Windows 設定工具下載、執行說明，以及已綁定電腦清單。

2026-10-24 原始碼新增可選 Codex CLI：[完整整合設計](company-ai-codex-design.md)。
`/policy` 新增 `codex_enabled`、`codex_models`、`codex_release`；缺省視為未開放。
僅在 `codex_enabled=true` 時提供 `-Tools Codex -InstallCodex` 與
`-Tools Both -InstallClaude -InstallCodex` 指令選項。公司 Codex 設定後完整關閉並重開終端機，再執行 `codex`。
新版安裝器會設定 User `CODEX_HOME`；原有個人設定檔保留。既有安裝可透過
`-Tools Codex -RepairPathOnly` 切換，不需要重新核准。
一般裝置核准與撤銷流程不變，不增加任何 Flutter 領取金鑰操作。
正式站 1.1.6 修正綁定頁未登入時停留空白資訊的流程，Codex 維持 0.164.0。內建頁會自動前往 BFF SSO 一次，保留完整 `request_id`／`user_code`，登入後仍需員工明確核准。若 SSO 未完成，可按登入或重新讀取；超過 10 分鐘須重跑 PS1 取得新碼。Flutter 無須改版即可使用內建頁；未來自建頁需保留相同行為。
1.1.5 先安裝 CLI 才開始裝置核准，防止大型下載耗盡交付／ACK 期限；Flutter 核准流程不變。
1.1.7 在公司 Codex 設定加入 `[features] daemon_auto_start = false`，避開長 Windows 使用者路徑的 socket 限制。既有員工下載新版 PS1 後使用 `-Tools Codex -RefreshModelsOnly` 即可套用，保留裝置綁定、憑證與對話，不必更新 Flutter。
1.1.8 的 PS1 預設同時選 Claude＋Codex，只安裝缺少的工具；一般使用不必再附 `-Tools Both -InstallClaude -InstallCodex`。內建核准頁也預設 Both；單一工具使用 `-Tools Claude`／`-Tools Codex`。既有 Flutter 下載連結與控制 API 無須變更。
1.1.4 修正官方 installer 下載的 HTTP 302；員工需要重新下載覆蓋舊版 PS1。
若 `/policy` 完全沒有 `codex_enabled`／`codex_models`／`codex_release`，表示此 BFF 路由仍使用舊版 API。
1.1.3 的 native `device/start` 另回傳 `codex_enabled`，讓 PS1 在開啟核准頁前確認後端版本與啟用狀態；
Flutter 不需呼叫或解析這個 native 端點。
Codex 模型已對齊 Claude：`gemini-3.8-flash`（預設）、`gemini-3.5-flash-lite`、`gemini-4-argon`。
啟用時 `codex_models` 與 `models` 完全相同且同序，前端不用維護另一份 Codex 模型清單。

- 清單顯示 `label`、`status`、最近使用時間、租約到期時間與重新確認期限。
- `expires_at` 是後端會自動延長的租約；不要用它提示員工每 30 天重新安裝。
- `hard_expires_at` 是必須回網站重新核准的期限。建議剩 7 天開始提示「重新設定此電腦」。
- 提示文案：「日常有效期由公司自動續期。到重新確認期限時，請再執行設定工具並核准這台電腦。」
- 撤銷電腦前顯示確認，成功後重新讀取清單。後端回覆 `upstream_block_pending=true` 時，顯示「已停止新請求，伺服器正在同步撤銷」。
- 欄位為 null 或缺省時顯示「尚未使用」等適當文案；所有整數時間皆為 UTC Unix **秒**，Flutter 顯示時轉使用者時區。

### 電腦核准頁

設定工具會開啟含 `request_id` 與 `user_code` 的網址。Flutter 頁面必須保留這些 query 參數，並在登入後回到同一個流程。

1. 用 `request_id` 呼叫 `GET /enrollments/{request_id}`。
2. 顯示伺服器回傳的電腦名稱與驗證碼，請員工與自己終端機上的碼比對。
3. 明確提示「只核准你自己剛剛啟動的設定」；query 中有 code 不代表應自動核准。
4. 員工按下「驗證碼相符，核准此電腦」後送出 approve。
5. 顯示「已核准，請回 Windows 設定工具完成設定」。Windows 工具自行領取及確認，不由 Flutter 輪詢領取 key。

若 `replace_device_id` 非空，提示「新設定完成後，這台電腦的舊金鑰會被撤銷」。不要在核准時自行撤銷舊電腦，後端會等新設定寫入成功後才切換。

## API 契約

使用 [OpenAPI JSON](company-ai-access-openapi.json) 產生或對照 Dart 型別。下表中的路徑都接在 API 前綴後。

| 方法與路徑 | 用途 | 瀏覽器要求 |
| --- | --- | --- |
| `GET /policy` | 模型清單、期限、工具下載位置 | 已登入 |
| `GET /devices` | 本人的電腦清單 | 已登入 |
| `GET /enrollments/{request_id}` | 讀取待核准資訊 | 已登入 |
| `POST /enrollments/{request_id}/approve` | 核准綁定 | 已登入，`Origin` 與 `X-Company-AI-Action: 1` |
| `POST /devices/{device_id}/revoke` | 撤銷本人的電腦 | 同上，body 可為 `{}` |
| `GET /admin/devices` | 管理員清單 | dev 管理員且具有當前 IdP 管理角色 |
| `POST /admin/devices/{device_id}/revoke` | 管理員撤銷 | 同上及 mutation headers |

`POST /device/start`、`/device/poll`、`/device/ack` 是 **Windows 工具** 的端點。Flutter 不可呼叫 poll、不可接收或保存 `key`，也不可要求員工填入 API key。

核准請求：

```http
POST /api/company-ai/enrollments/REQUEST_UUID/approve
Content-Type: application/json
X-Company-AI-Action: 1
Origin: https://ai-dev.changdar-server.mooo.com

{"user_code":"AB12-CD34","confirm":true}
```

成功回應：

```json
{"device_id":"DEVICE_UUID","status":"approved"}
```

瀏覽器 `Origin` 由瀏覽器自動提供，不要嘗試從 Dart 偽造它。使用 same-origin cookies，`credentials: include` 或等效設定。ai-dev 不需要讀取或保存 Open WebUI token。

若 Flutter 有 service worker／離線快取，必須排除所有控制 API 與綁定回應；不要快取員工資料、核准結果或任何 native 領取回應。

`GET /devices` 回應範例：

```json
{
  "devices": [{
    "id": "DEVICE_UUID",
    "label": "WIN-ENGINEERING",
    "status": "active",
    "created_at": 1800000000,
    "expires_at": 1802592000,
    "hard_expires_at": 1807776000,
    "last_seen_at": 1800000100,
    "acked": true,
    "confirmation_required": false
  }]
}
```

錯誤回應格式：

```json
{"detail":{"code":"ai_access_enrollment_expired"}}
```

| 狀態 | UI 行為 |
| --- | --- |
| 401 | 請員工登入當前環境；保留 request_id，登入後重讀 |
| 403 | 顯示無使用權限；若是模型 API 的 `ai_access_network_denied`，請連公司 LAN／核准 VPN |
| 404 | 不存在或不屬於本人，停止操作 |
| 409 | 重複核准、電腦數量已滿或狀態衝突，重讀清單並顯示提示 |
| 410 | 此次設定已過期／已領取，請重新執行 Windows 工具 |
| 422 | 請求欄位驗證錯誤；模型 API 的 `ai_access_budget_exceeded` 是共用額度已用盡 |
| 429 | 依 Retry-After 等待，不持續快速重試 |
| 503 | 後端或 IdP 暫時不可用，保留頁面並讓員工重試 |

## 在另一台電腦開發和交付 ai-dev

Flutter 原始碼不在 A；請在你們自己的 repo 建立頁面，API 前綴做成可配置值，不把 dev 網址寫死在共用程式碼。

```text
dev browser API prefix: /api/company-ai
production browser API prefix: /bff/ai_access/api/company-ai
```

先將 build 部署到 ai-dev 同源位置。不要讓 Flutter localhost 跨源直接讀 ai-dev cookie；後端不會為此放寬 cookie/CORS。

```bash
flutter build web --base-href /company-ai/portal/
```

將 `build/web` 的內容透過公司核准的傳檔方式交給 A 的操作者，安裝到：

```text
/home/changdar/.local/share/ai-gateway-dev/flutter-company-ai/
  index.html
  flutter_bootstrap.js
  assets/...
```

A 的 dev WebUI 已預留 `AI_ACCESS_FLUTTER_DIR` 指向此目錄；不要把 build 複製到正式 Flutter 站或覆蓋 Open WebUI 根目錄。完成後用 `https://ai-dev.changdar-server.mooo.com/company-ai/portal/` 測試。請按既有 Flutter web 的路由策略正確處理前綴和 query，並確保 `index.html` 是 build 根目錄的直接子檔。

核准頁未驗收前，Windows 工具仍開啟內建 `/company-ai/setup`。Flutter 完成後，A 操作者將私密 `ai-access.json` 的 `AI_ACCESS_APPROVAL_PATH` 改為 `/company-ai/portal/`，重啟 `ai-gateway-dev-ai-access`。不需要改 Windows 工具。

若工程師希望本機 hot reload，需使用公司既有的同源開發代理，讓登入與全部 API 維持同一個瀏覽器 origin；不要把 session token 放進程式碼、URL、localStorage 或截圖。

## Windows 驗收

1. 使用一般員工 Windows 帳號，在非系統管理員 PowerShell 執行設定工具；電腦需能以有效 HTTPS 憑證連到內網入口。
2. 從設定頁下載 `Setup-CompanyAI.ps1`，依公司腳本簽署／執行政策執行：

```powershell
.\Setup-CompanyAI.ps1 -Portal https://ai-dev.changdar-server.mooo.com -InstallClaude
```

3. 比對終端機及網頁驗證碼，再核准。確認終端機未顯示金鑰。
4. 檢查 `%USERPROFILE%\.claude\settings.json` 保留既有設定，只有 helper 路徑、模型及 base URL；沒有公司的明文 key。
5. 關閉舊終端機，開新終端機執行 `claude`，用 `/status` 確認公司 base URL 與 helper。不要把 helper stdout 貼到聊天或日誌。
6. 實際進行非串流／串流對話、模型切換及一個工具呼叫。模型供應商 quota 或 dev 額度不足時，先由後端處理，不修改網頁跳過預算。
7. 再跑一次設定，確認只有新設定完成後舊 key 才失效。ACK 送出前的失敗應回復舊設定；若 ACK 已送出但所有回應遺失，工具應保留新設定並提示重新執行，不能保證舊 key 仍可用。
8. 從頁面撤銷電腦，確認新模型請求被拒；用測試帳號移除 dev 角色，確認在 30 秒狀態快取到期後被拒。不要用正式員工帳號測試停權。
9. 在隔離的測試狀態中模擬續期閾值與 90 天期限；不要改正式主機時鐘或既有員工預算。自動測試已涵蓋這些時序。

Windows 腳本不會繞過 ExecutionPolicy；若公司政策阻擋，請交 IT 簽署／核准工具。`tests/windows/Test-CompanyAI.ps1` 可在核准的 Windows 環境檢查 helper 的 DPAPI、截止期限及設定合併行為。

Codex 另驗收固定版本原生安裝、公司 home 與個人設定隔離、`codex` 的 `/status`、
模型切換、完整工具結果回傳、長對話本機壓縮，以及公司／HTTPS 路徑的費用歸戶。
先以 `-Tools Codex` 確認不依賴 Claude，再以 `-Tools Both` 驗證一次核准可供兩種工具使用，
並驗證 ACK 前失敗時，憑證、兩種客戶端設定與 helper 都恢復原始 bytes。

公司 LAN 範圍目前依 B 的 `192.168.1.88/24` 設為 `192.168.1.0/24`，並拒絕路由器 `192.168.1.1` 作為來源。若 DNS 走外部 hairpin，請由公司 DNS 將 ai-dev 網域解析至 B 的內網 IP。VPN 需另外提供並核准測試 Windows 的實際 peer IP；B 的 Tailscale 位址不是員工 VPN 網段。

## 正式公司網站的差異

正式 Flutter 的瀏覽器控制 API 走既有 BFF `/bff/ai_access/api/company-ai`，沿用公司 BFF cookie。寫入請求同時加上既有 `X-CSRF-Token` 和 `X-Company-AI-Action: 1`。前端不直接把 OIDC access token 傳給 LiteLLM。

Windows 工具的 native 端點、下載位置和模型入口由 B 的 Nginx 發布；正式 approval path 使用 `/company-ai/portal/`，由公司的 Flutter app 呈現。這些路由需由後端操作者正式部署後才切換。不要把 dev 的 registry、金鑰或 directory client secret 搬到正式環境。

每日用量畫面沿用既有 AI usage API。後端仍以相同 Keycloak `sub` 記帳，因此 Open WebUI、Claude Code 和已啟用的 Codex 共用額度；Flutter 不自行以各把 key 計算另一份餘額。

## 可選 MCP metadata（2026-11-15，預設停用）

`Policy`、native `/v1/models` 與 `/v1/codex/config` 可新增 `mcp`；未配置時為 null。
公開欄位只有 version、固定 HTTPS資源 url、issuer、scope、resource_mode、
Codex／Claude public client IDs與固定 callback ports，不包含來源帳密、使用者 token
或 introspection secret。完整欄位見 `company-ai-access-openapi.json` 的 `MCPPolicy`。
舊客戶端可忽略此可選欄位；BFF沿用既有驗證與轉送，不替 CLI完成資源 OAuth。

新版 Company AI設定腳本會合併固定公司 MCP entry；第一次讀取公司資源需完成各 CLI
的原生 OAuth。其權限預設包含本人所有有效保存綁定及來源 ACL；
NAS／EIP密碼仍只由後端連接器解密使用，Flutter與 CLI不接收來源憑證。
EIP新 `save_credentials` 是資源服務可選欄位，不是 Flutter／Visionnaire登入或 refresh契約。
本輪沒有更新正式 Flutter、下載 EXE或實際 IdP設定。
