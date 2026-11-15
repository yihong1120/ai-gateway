# Company AI Access

安裝、測試與發布統一使用根目錄的 [開發指南](../../CONTRIBUTING.md)。

為公司 Claude Code／Codex 提供裝置核准、限期金鑰與 LiteLLM 轉送。
使用者透過既有網站登入並核准裝置；本服務不建立另一套使用者登入或 refresh token。

## 目錄與責任

| 位置 | 責任 |
| --- | --- |
| `app.py`、`runtime.py` | FastAPI 組裝及 PostgreSQL／HTTP／背景維護生命週期 |
| `config.py` | 啟動時驗證的設定快照 |
| `routers/`、`schemas.py` | HTTP 邊界、輸入與回應契約 |
| `services/` | 裝置核准、交付、續期、撤銷及串流轉送 |
| `protocols/` | Anthropic／Responses 請求篩選及 Codex catalog |
| `adapters/` | LiteLLM、Keycloak directory 與 WebUI bridge |
| `storage/` | 非同步 PostgreSQL repository、加密金鑰及 schema 初始化 |
| `windows/`、`ui/` | 員工端設定工具及核准頁 |
| `cli/` | 管理、來源發布包、API 契約匯出及舊資料匯入 |
| `deployment/` | 正式部署、回復、Windows EXE 與簽署工具 |

使用者認證核心在 `examples.identity`；裝置憑證是獨立的權限與生命週期。
服務建立時固定持有設定、repository、LiteLLM、directory 與 HTTP client。
請求不重新讀取 bridge／BFF 證明金鑰或解析網段；變更設定後需重新啟動。

## 啟動與管理

以根目錄 [config.example.toml](../../config.example.toml) 的 `[company_ai_access]`
準備私人 `config.local.toml`。必要項目包括 HTTPS origin、
本機 PostgreSQL、狀態目錄、LiteLLM master key、模型清單與 directory 設定。
`AI_ACCESS_INFERENCE_CIDRS` 必須明確允許 LAN／VPN；拒絕網段優先。

```bash
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m uvicorn examples.company_ai_access.app:create_app --factory --host 127.0.0.1 --port 8013 --workers 1
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.ops status
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.ops maintain
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.ops revoke-user SUBJECT
```

管理命令也會取得 registry lease，執行前需先停止使用同一 registry 的 API。
WebUI 使用 `adapters.webui.install_webui` 接入既有登入，並透過私密 loopback bridge 轉送。
獨立 API 可使用固定 issuer／audience 的 OIDC token；BFF 另外提供來源證明。
來源 header 不能取代使用者身分或裝置金鑰。

## 裝置與成果一致性

1. Windows 工具先完成 CLI 安裝，再建立 PKCE 核准請求。
2. 使用者登入網站、比對代碼並明確核准；服務決定帳號與可用模型。
3. 原始金鑰先加密保存，再向 LiteLLM 建立相同金鑰，供交付中斷後核對。
4. 工具使用目前使用者的 DPAPI 保存憑證；ACK 成功後才撤銷被替換的舊裝置。
5. 未確認交付、到期或被撤銷的裝置不能續期。上游封鎖失敗會保留待重試狀態。

預設 lease 30 天，最晚在首次核准後 90 天重新核准；directory 故障不延長 lease。
撤銷先在本機拒絕新請求，再封鎖上游；不會中斷已送出的模型串流。
金鑰屬 bearer credential，裝置名稱不是硬體綁定。
LiteLLM 用不可變的使用者 `sub` 彙總網站與 CLI 用量；並行用量不是金額預約，
預算上限不保證所有在途請求合計恰好停在指定金額。

## PostgreSQL 與備份

`AI_ACCESS_DATABASE_URL` 僅接受 loopback／本機 Unix socket，沒有 SQLite fallback。
`AI_ACCESS_DB_SCHEMA` 預設為 `company_ai_access`；開發環境使用獨立 schema 與加密金鑰。
`AI_ACCESS_STATE_DIR` 必須是私密目錄，保存 `access.key` 與本機 worker lock。

備份 PostgreSQL schema、對應的 `access.key` 及私密設定。既有 registry 不可重建金鑰；
一般程式回復不可倒灌舊 registry，避免恢復已撤銷憑證。
僅仍持有舊 SQLite registry 的部署需要先停止服務，再執行：

```bash
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.migrate          # 唯讀預覽
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.migrate --apply  # 匯入空白 PostgreSQL 目標
```

匯入保留原金鑰與密文，原始檔案不變；相同快照可重試。PostgreSQL 接受新寫入後，
不可再用舊 SQLite 檔案當作目前資料庫。

## 發布與驗證

```bash
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.release --help
uv run --frozen --no-dev --extra company-ai-access ai-gateway exec company_ai_access -- python -m examples.company_ai_access.cli.export_contract
```

來源發布包使用單一產生器，不再另外產生歷史 handoff ZIP。
Windows 腳本與 EXE 必須走 [簽署流程](deployment/windows/README.md)；
正式更新與回復見 [部署文件](deployment/production/README.md)。
Codex 啟用需指定核准 CLI 版本與符合完整模型清單的 catalog。

對外契約見 [OpenAPI](../../docs/company-ai-access-openapi.json)、
[Flutter 整合](../../docs/company-ai-access-flutter-handoff.md)及
[Codex 設計](../../docs/company-ai-codex-design.md)。歷史驗證證據保留於 repo 的 `docs/`，
不再混入每次來源發布包或當作目前執行狀態。

### 公司資源 MCP 候選整合（1.3.0）

新版 `windows/Setup-CompanyAI.ps1` 支援伺服器可選 `mcp` 公開政策，
合併 Codex公司 home與 Claude的固定公司 MCP entry，保留其他 MCP設定，
並將更動納入安裝失敗回復。沒有 MCP政策的既有後端維持原流程。
首次使用各 CLI資源時走原生公司 OAuth；模型裝置金鑰與 MCP token分開管理。

後端實作在 `company_mcp/`，預設停用；完整配置、schema升級、
Keycloak public clients／introspection與發布檢查見
[公司 CLI設計](../../docs/company-ai-codex-design.md)。
來源候選版本1.3.0尚未簽署或部署；必須依既有流程重新簽署 PS1、建置並簽署 EXE，
完成 Windows與實際 OAuth／refresh驗收後，才能替換正式下載。
