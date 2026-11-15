# 公司助理的外部唯讀 MCP 工具

Gateway 提供通用 MCP 連接、個人 OIDC 身分轉接及 Agent 工具組裝。
來源服務維護工具名稱、描述、JSON schema、資料查詢、領域授權與公開 provenance。
Gateway 不複製來源服務的資料模型、查詢欄位或業務規則，也不跨 repo 匯入 Python 模組。

## 設定

在既有 `COMPANY_AGENT_CONFIG` JSON 設定加入以下欄位，其餘設定保留：

```json
{
  "mcp_servers": [
    {
      "name": "records",
      "url": "https://records.example/mcp",
      "allowed_tools": ["list_items"]
    }
  ]
}
```

這是設定片段，不是完整檔案。名稱與工具必須依來源服務提供的契約填寫。
私人 `config.local.toml` 的 `[webui]` 以 `COMPANY_AGENT_CONFIG` 指向完整 JSON。
預設 `mcp_servers=[]`，不發送探索請求、不取得個人 token。
舊的領域專用 `construction_api_url` 已移除；設定不再接受這個欄位。

每個來源需有唯一名稱、固定 HTTPS endpoint 及明確工具允許清單；HTTP 僅允許本機
loopback 測試。URL 不接受帳密、query、fragment 或路徑穿越。設定由管理員維護，
來源必須受信任並接受目前使用者的 OIDC token，不能由聊天或模型指定。
工具服務的 issuer、audience 與使用者權限配置由服務維護者提供。

## 執行與權限

每次工作開始先以本人身分進行 MCP 探索，取得工具 JSON schema。
只有清單明確允許、且來源宣告 `readOnlyHint=true`、`destructiveHint=false` 的工具
才交給 knowledge 角色。這些標註用於篩選；真正的資料授權仍由來源服務執行。
主協調透過 `delegate_knowledge` 使用，不直接取得外部工具或 token。

執行名稱為 `external_<server>_<tool>`，過長時以穩定雜湊縮短到模型名稱上限。
工具描述與參數直接取自來源；gateway 不建立領域專用 schema。
`runtime`、`specification` 是執行時保留欄位，來源 schema 與模型輸入不能使用。

每次探索與呼叫都重新確認 WebUI 使用者及 bound OIDC subject，選取本人最新的
OIDC session，由宿主取得／更新 access token。不以較舊 session、服務帳號或
LiteLLM key 備援。未驗簽 JWT sub 解析僅用於拒絕換綁後的舊身分；來源服務仍須
驗證簽章、issuer、audience、撤銷、帳號與每次資料存取權限。
憑證只存在執行時，不進模型參數、工作 payload、回條或 checkpoint。

每次呼叫在新的授權 session 重新列工具，核對設定、schema、描述與標註指紋後才執行。
恢復工作時也核對來源、允許清單及契約指紋，變更後要求另開工作。
沒有自動重試、跨版本備援、轉址、sampling、elicitation 或來源要求的客戶端執行。
HTTP 回應上限 1 MiB、schema 64 KiB，並限制工具目錄大小及分頁。
連線與 SDK session 隨單次操作關閉；工作取消正常傳播。

工具錯誤只提供安全限制，失敗不當成空清單或查無資料。來源內容、schema 描述與
provenance 均屬資料，不授予額外操作權限。這個 connector 僅支援唯讀文字／結構化結果。

## 來源與結果

成功回條包含固定 source、server、tool、查詢時間及原始工具結果。
來源可在結構化結果的 `provenance` 提供不含憑證的公開查詢證據，例如涵蓋範圍、
分頁、紀錄識別碼與限制。Gateway 將最多 8 KiB 的 provenance 作為不透明資料轉送，
不解讀其中的領域欄位。超過上限時標示 `provenance_truncated`。
主協調委派回條與公開工作結果保留這些 metadata，不包含子 Agent 原始對話或整份資料。

## 驗證

依根目錄開發指南準備環境，再執行：

```bash
python -m unittest discover -s company_agent/tests -p 'test_external_mcp_*.py' -v
```

Graph 與恢復工作測試需隔離的 `COMPANY_AGENT_TEST_DATABASE_URL`；可使用現有離線驗證器
建立暫存 PostgreSQL。一般測試以 HTTP／OIDC／模型替身執行，不連接正式來源。
MCP SDK 與 jsonschema 由根目錄 company-agent 安裝組合宣告並在 `uv.lock` 鎖定。
