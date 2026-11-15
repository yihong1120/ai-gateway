# Company AI 部署與回復

本目錄管理獨立 Company AI Access API 的部署；LiteLLM 設定由
[examples/litellm](../../../litellm/README.md) 管理。
Windows EXE／腳本簽署見 [Windows 文件](../windows/README.md)。

## 工具分工

| 工具 | 用途 |
| --- | --- |
| `upgrade.py` | 既有服務更新、備份與失敗回復 |
| `run.py` | 從服務私密設定建立執行環境 |
| `smoke.py` | 部署後 HTTP／身分／裝置生命週期驗證 |
| `internal_ca.py`、`install_internal_ca.py` | 內部 Code Signing CA 建立與安裝 |
| `smoke_internal_ca.py` | CA／CRL 發布驗證 |
| `signed_release.py`、`publish_installer_input.py` | 簽署腳本驗證及 EXE 簽署輸入發布 |

本目錄只維護既有部署的更新；原本綁定歷史主機的首次啟用腳本、Nginx 快照及
重複模型 policy 已移除。首次安裝需依根目錄 `config.example.toml` 的 `[company_ai_access]` 準備
私人設定、建立服務及設定反向代理，
本目錄不提供自動首次佈建。既有部署使用 `upgrade.py --help` 查看必要參數。
啟動器只接受目前來源包的目錄結構；舊發布包保留其自己的啟動器與回復收條。

## 來源包

```bash
python -m examples.company_ai_access.cli.release --help
python -m examples.bff.deployment /path/to/staged-release
```

Company AI release 只包含目前來源、測試與整合契約，不附帶歷史驗證報告。
BFF 產生器建立 `identity-services.zip` 與 `bff-artifact.json`，包含完整依賴及
逐檔 SHA-256；部署時一起驗證，不另維護 `bff-proxy.py` 副本。
新來源以雜湊版本目錄保存，服務切換透過有備份的 systemd drop-in。

## 資料與回復

服務統一使用本機 PostgreSQL；備份對應 schema、`access.key` 及私密設定。
`upgrade.py` 保存 schema 的 custom-format `pg_dump` 及設定／金鑰備份。
一般更新回復只還原程式、設定與服務入口，不還原 registry，以免重新啟用已撤銷裝置。
舊 SQLite 匯入方式見 [產品文件](../../README.md#postgresql-與備份)。

檢查 API、BFF、Nginx 與 CA／CRL 健康；裝置 smoke 產生的測試金鑰需撤銷。

`smoke.py --config /private/config.local.toml` 從專屬 `[company_ai_smoke]` 區段讀取
`KC_BOOTSTRAP_ADMIN_USERNAME`、`KC_BOOTSTRAP_ADMIN_PASSWORD`；清理測試模型金鑰使用同次載入的
`[litellm]` 快照。這組操作員憑證不屬於服務執行設定，不可用唯讀目錄帳號代替，也不再讀取
舊的 `production-keycloak.env`。不要將此區段投影到 WebUI、公司 AI API 或一般 SDK 程序。
實際執行的 release 路徑及驗證收條以目標主機為準，README 不宣告主機目前版本。
歷史部署證據保留於根目錄 `docs/company-ai-*-production-validation-*.json`。
