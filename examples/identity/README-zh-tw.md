# 使用者與認證核心

安裝、測試與發布統一使用根目錄的 [開發指南](../../CONTRIBUTING.md)。

本套件統一使用者、登入、授權與 token/session 實作。原 `examples/auth` 與管理應用中
對應的路由、schemas、服務已集中到這裡，不保留舊命名空間轉接。

| 位置 | 責任 |
|---|---|
| `accounts/` | 帳號 CRUD／管理、密碼、驗證信、恢復、同意紀錄與帳號連結 |
| `sessions/` | 登入、JWT 簽發／驗證、原子 refresh、撤銷、瀏覽器 session 儲存與清理 |
| `providers/` | Keycloak／OIDC、社群驗證與原生登入交易 |
| `authorization/` | 群組、功能權限與站點成員預設權限 |
| `routers/`、`schemas/` | 對外 HTTP 邊界與資料契約 |
| `settings/`、`config.py` | 每個應用一次建立的具型別設定快照 |
| `runtime.py`、`lifespan.py` | 應用持有的認證依賴及資源啟閉 |
| `models/`、`database.py` | 沿用的唯一 ORM metadata 與資料庫連線生命週期 |
| `redis_pool.py`、`redis_counters.py`、`cache.py` | 共用 Redis、原子限流與帳號快取 |

本套件沒有獨立啟動第二個帳號服務。`examples.db_management.app:create_app` 掛載這些
router，既有 API 路徑、cookie、claims、環境變數及資料表不因模組搬移而改名。
`examples.bff` 保留瀏覽器導向、cookie／CSRF 與代理邊界；使用共同的身份及 session 核心。
播放服務和站點媒體清理由管理應用組裝，不由 identity 核心匯入或建立。

`models/` 暫時保留原先共用的商務模型註冊入口，避免產生第二份 Base／metadata；
不會為搬移套用資料庫 migration。共用 SQLAlchemy／Redis 資源仍由應用 lifespan 管理。

## Token 邊界

- 本機帳密模式：PostgreSQL 一次性恢復／驗證 token；Redis 原子 session 與 refresh 重用偵測。
- OIDC 模式：由設定的 Keycloak／OIDC 身份供應者管理登入權威，本套件驗證與連結身份。
- BFF session：共用核心儲存，瀏覽器 adapter 處理 cookie、CSRF、導向與代理更新。
- 公司 AI 裝置憑證：仍由 `examples.company_ai_access` 管理；不混入使用者 refresh family。

## 維護與驗證

涉及登入、恢復、refresh 或身份契約，需同步檢查相鄰後端與 Flutter；範圍見
[跨 repo 契約](../../docs/authentication-cross-repo.md)。Keycloak-only 後端不搬入本機密碼服務。
完整離線回歸使用 [開發指南](../../CONTRIBUTING.md#離線驗證) 的統一命令。
服務的環境與啟動方式見 [examples 總覽](../README.md)。
