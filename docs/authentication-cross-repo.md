# 跨 repo 認證契約

帳號、密碼恢復、信箱驗證、refresh／session 的修改需一併檢查下列實際維護中的 repo。

| Repo | 登入權威與同步責任 |
| --- | --- |
| ai-gateway | 本機帳密模式使用 PostgreSQL 一次性 token、原子 Redis session；OIDC 模式由設定的身份供應者管理。 |
| Construction-Hazard-Detection | 已移除本機密碼登入與恢復，使用 Keycloak／OIDC；共用限流、撤銷與客戶端契約，不套用 gateway 的 identity migration。 |
| visionnaire-flutter-dev | 依部署登入模式處理 BFF／PKCE 或既有本機入口；保留寄信待確認結果、正確分類 refresh 錯誤，不自行重送一次性操作。 |

偵測後端的原生社群登入與 API 配額使用 `examples.auth.redis_counters` 的固定視窗 Lua，
與 gateway 的 `examples.identity.redis_counters` 保持相同實作。計數與 TTL 原子提交，
既有無 TTL 的 key 補期限但不重設計數。
不要以同步檔案為由覆蓋各 repo 不同的認證組裝、Keycloak 管理流程或尚未提交的修改。

Flutter 的 `VerificationEmailResult` 保留 `verification_email_pending`，登入頁及驗證頁
顯示「寄送結果尚待確認」，不直接宣稱寄送成功，也不自動重寄。文案涵蓋現有八種語系。
HTTP 503 `database_error` 保留原錯誤碼及表單重試能力，不自動重送密碼更新。

Refresh 契約：401 需重新登入；409 只有 `deployment_configuration_changed` 終止 session；
400 的 invalid／expired／revoked／reused refresh 錯誤亦終止 session。
403、429、逾時及 5xx 保留 session，即使回應帶有相似的 provider 錯誤名稱也不清除。

Identity 收斂回歸：gateway unittest 1,852 通過、11 略過；pytest／原生 CLI 84 通過、
2 略過。乾淨 sdist／wheel、隔離匯入與部署包檢查通過；歷史封存實體檔案核對未啟用。
偵測後端認證／社群登入／路由測試 96 通過；Flutter 服務、session、密碼及驗證信畫面
測試 82 通過、1 略過。資料庫與 Redis 測試使用隔離資源；原有 pubspec.lock 保留。
沒有部署、套用正式 migration、呼叫真實身份供應者或寄送郵件。

## Identity 核心收斂

Gateway 的帳號、session、provider 與授權實作集中在 `examples.identity`；
BFF 與管理應用使用同一個核心，舊 `examples.auth` 與管理應用內對應模組已移除。
資料表、cookie、JWT claims、登入／refresh 路徑不變；搬移前後的完整 OpenAPI paths
與 components 已比對一致，不需要為 Python 命名空間搬移更動 Flutter API。
Keycloak-only 後端保留自己的 `examples.auth` 及身份權威；共用限流 Lua 保持同一契約。
播放與媒體清理由管理應用組裝，不再由 gateway identity 核心建立。

## WebUI 身分與用量邊界收斂（2026-11-11）

本輪集中 WebUI 可信 OIDC subject 的取得、LiteLLM 用量歸屬與用量呈現責任。
身分仍由伺服器已驗證的 WebUI 使用者及其 `oauth.oidc.sub` 提供；
LiteLLM `user`／`end_user`、登入／session、HTTP 路徑與回應契約維持既有行為。
宿主 `verified_user` resolver 從 `examples/openwebui/identity.py` 搬至
`examples/identity/adapters/webui.py`，保留逐次延遲呼叫宿主
`get_verified_user_by_id`、缺少使用者時的 ValueError 及 `model_dump()` 行為。

偵測後端沿用自己的 JWKS／issuer／audience 驗證與 `UserIdentity(provider, sub)`
權限對應；BFF 保留 server-side bearer 轉送與 `ai_usage` allowlist。
Flutter 沿用 Web cookie／CSRF、Native Bearer 及既有 refresh 錯誤分類。
兩個 sibling 沒有引用舊 resolver 匯入或檔案路徑，程式保持原樣；純搬移後重新執行相同契約回歸。

適用回歸已執行：

- 偵測後端的 OIDC、identity provider、BFF OIDC、proxy 四組測試：67 項及 11 個 subtest 通過。
- Flutter 的 auth headers、session、failure policy、auth provider 四組測試：61 項通過，
  1 項 Web-only case 在預設 VM 執行環境略過。

測試使用合成設定與外部服務替身，停用 dotenv 自動載入；Flutter 使用 `--no-pub`。
兩個 sibling 的測試前後 Git 狀態完全一致，`pubspec.lock` 位元組保持不變。
目前 Flutter repo 尚無用量 API／頁面的實作；上述回歸涵蓋既有認證傳輸與 session，
不宣稱已完成用量頁串接。沒有執行部署、資料庫 migration 或真實身份供應者呼叫。

## 公司資源 MCP 與既有認證邊界（2026-11-15）

Gateway新增預設停用的 `/company-mcp` 資源服務，使用公司原生 public-client OAuth、
固定資源 audience、PKCE S256及後端 live introspection。它不發放本機密碼 token，
不修改既有登入／refresh／session。不可用模型裝置 key、WebUI cookie或 Visionnaire
API token替代資源 token；WebUI owner僅由唯一且仍有效的 `oauth.oidc.sub` 對應。
公司模型政策新增可選 `mcp` 公開 metadata；EIP `/link` 新增可選 `save_credentials`，
省略保持舊契約，明確保存不等於啟用自動同步。

已同步檢查兩個 sibling的 client／BFF／認證程式並更新既有契約文件；
偵測後端仍以 Keycloak為身份權威，新增 MCP audience不能用於 Visionnaire API的測試。
Flutter尚無 Company AI policy／NAS／EIP型別客戶端，BFF保留其既有不透明轉送，
因此無須新增 Flutter登入、token表或 migration。既有未提交變更均保留。

適用回歸：偵測後端68項及11個 subtest通過；Flutter70項通過、1項 Web-only略過，
使用 `--no-pub`，沒有修改 Flutter依賴。Gateway另以實際 WebUI 0.13.3及暫存 SQLite
驗證8項宿主身分／callback測試。所有外部 provider與憑證皆為合成或替身，
沒有修改正式 Keycloak、帳密、session或執行部署。完整紀錄見
`company-mcp-implementation-validation-2026-11-15.json`。

## 公司 MCP模組與生命週期重構（2026-11-15）

資源實作移至獨立 `company_mcp/`，由 `examples.openwebui.app` 的 adapter組裝；
官方 SDK由低階 `Server` 改用高階 `MCPServer`。唯一 WebUI owner查詢移至
`company_mcp.adapters.webui`，token verifier改為必須注入可信 resolver。
共用同站 body與 private response處理移至 `examples.shared.runtime.browser`，
原 cookie、CSRF header、大小限制、EIP保存選項及原生 MCP HTTP契約保持不變。

已再次檢查兩個 sibling，更新既有契約文件；其 Keycloak身份權威、登入、refresh、
BFF與 Flutter客戶端均無適用的程式變更。重跑同組 OIDC／BFF及 Flutter認證回歸，
結果與未提交變更保留摘要見 `company-mcp-refactor-validation-2026-11-15.json`。

## 獨立 MCP程序與私有資源authority（2026-11-15）

MCP改為獨立 ASGI程序，公開 resource URL／scope／audience及 CLI policy不變。
新增的 loopback `/internal/company-mcp` 是同一 MCP資源的私有驗證／執行元件：
需要專用 workload key及本人資源 assertion；後端親自驗證原 issuer、audience、
client、scope、live session及唯一 OIDC owner，服務key本身沒有員工資源權限。
原生 token在 authority終止，不用於 NAS／EIP、模型代理或另一個 OAuth資源。
此端點不得加入 Nginx公開路徑或 BFF轉送白名單，沒有新使用者登入／token／migration。

已同步檢查兩個 sibling並更新既有契約說明；其 Keycloak身份權威、Flutter/BFF
登入與 refresh不需程式修改。資源後端離線回503，不應當作公司登入失效或自動重播。
回歸與保留既有變更證據見 `company-mcp-service-validation-2026-11-15.json`。

## MCP函式結構整理（2026-11-15）

僅將 MCP生命週期、資源authority、授權回呼、私有回應包裝及測試的巢狀函式
改為模組函式、類別方法或 partial。HTTP路徑、cookie、CSRF、身分驗證、
來源 ACL與資料庫結構保持原契約；兩個 sibling沒有適用的程式或客戶端修改。
核對及回歸證據見 `company-mcp-structure-validation-2026-11-15.json`。

## MCP 內網部署準備（2026-11-15）

正式 realm 僅新增兩個 MCP public client、專用 confidential introspection client 與
選用的 `company.resources` scope，未修改既有 realm、client、使用者或預設 scope。
MCP token 的 audience 僅包含固定 MCP URL 與該資源的專用驗證 client，
不包含 `visionnaire-api`，不關閉 Keycloak 的 introspection audience 檢查。
LAN／核准 NetBird 來源限制在 Nginx 與 MCP 執行，公開 BFF 白名單不加入私有 authority。

再次核對 sibling 的 OIDC、identity provider、BFF proxy、Flutter headers／session／
failure policy／native envelope；後端 68 項與 11 個子測試通過，Flutter 65 項通過、
1 項 Web-only 略過。兩個 repo 原有 Git 狀態、既有未提交檔案及依賴鎖檔位元組均未變，
無須把 gateway 的本機密碼或 MCP token 表搬入其他身份權威。
部署機 105 項功能測試與 6 個子測試通過，正式服務已於 2026-11-15 11:00（台北）切換。
部署後核對 schema 6／12、既有資料與原始金鑰保留，LAN 要求 OAuth，
非核准 Tailscale 來源含偽造轉送標頭仍回 403；未更動其他身份權威或登入 session 契約。
詳細狀態與驗證證據見 `company-mcp-deployment-2026-11-15.json`。

## Python import 整理（2026-11-15）

依賴宣告集中於檔案上方，WebUI 仍先載入專用設定才組裝宿主；選用的 worker、
循環依賴及測試 fixture 保留原有載入邊界。未新增登入模式、token、migration 或修改
HTTP 契約。整理前後管理 API 的完整 OpenAPI 相同（81 條路徑、93 個 schema）。
兩個 sibling 的 OIDC／BFF 與 Flutter session 契約已重新檢查：後端 68 項及 11 個
子測試通過，Flutter 65 項通過、1 項 Web-only 略過，無適用的客戶端修改。
驗證與既有未提交變更保留證據見 `import-layout-validation-2026-11-15.json`。
