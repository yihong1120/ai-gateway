# AI Gateway

LiteLLM 模型代理、Open WebUI 整合、公司助理及公文工作流。
服務與工具依產品放在 [examples](examples/README.md)；公司助理核心位於 `company_agent/`，
公司共用 NAS／EIP MCP獨立服務位於 `company_mcp/`。

## 安裝與啟動

所有 Python 依賴統一在根目錄 `pyproject.toml` 宣告、`uv.lock` 鎖定。
依服務選擇安裝組合；開發、測試及發布步驟見 [CONTRIBUTING.md](CONTRIBUTING.md)。

安裝 uv 後，從 repo 根目錄建立並啟用開發環境（以下使用 Linux bash／zsh）。
再從範本建立私人設定，填入實際憑證、端點及資料庫設定；已有設定時保留原檔：

```bash
uv sync --frozen --all-extras
source .venv/bin/activate
cp -n config.example.toml config.local.toml
chmod 600 config.local.toml
# 填入各服務的實際設定後：
ai-gateway check
ai-gateway compose litellm up -d
```

單一私人設定為 `config.local.toml`；既有環境的一次性轉存見 [開發指南](CONTRIBUTING.md#環境設定)。
完整模型設定、原生服務及 dev profile 見 [LiteLLM 文件](examples/litellm/README.md)。
Open WebUI 連至 `http://127.0.0.1:4000/v1`，使用配置的受限 virtual key。
私人環境、資料庫、金鑰與預算在開發及正式環境分開管理。

## 功能入口

- [服務總覽與安裝組合](examples/README.md)：identity、BFF、管理、用量與部署註冊。
- [公司 AI 裝置與 CLI](examples/company_ai_access/README.md)：核准、交付、續期與撤銷。
- [公司助理](company_agent/README.md)：背景工作、工具及工作台。
- [公司資源 MCP](docs/company-ai-codex-design.md)：獨立 `company_mcp/`、Codex／Claude原生 OAuth及本人資源；預設停用。
- [公文工作流](examples/official_document_workflow/README.md)：解析、文件輸出與 EIP。
- [Open WebUI 組裝](examples/openwebui/README.md)：宿主入口與 Function。
- [本機推論](examples/litellm/README.md#本機推論)、[OCR 1.6](docs/paddleocr-vl-service.md)：GPU 服務與後端設定。
- [部署與完整驗證](examples/deployment/README.md)：測試、發布包及隔離資源。

開發環境安裝後，完整離線驗證與 CI 使用相同命令：

```bash
uv run --frozen --all-extras python -m examples.deployment.tools.validate
```

模型、費率及政策以 `examples/litellm/config.yaml` 為準。`docs/` 保留設計與操作文件，
實驗輸出放在已忽略的 `artifacts/`；歷史數據不代表主機目前狀態。舊 TabbyAPI、OCR 1.5／PP-OCR 備援及
已完成的模型退役工具已移除；目前只維護對應產品目錄中的啟動設定。

## 架構與執行契約

應用持有各自資源，背景工作由 PostgreSQL lease 管理，同一帳號依序處理。
工作重啟不重播外部操作；已完成成果、待確認與結果不明分開保存。
並行名額、資料一致性及取消處理見 [架構說明](docs/runtime-refactor.md)，
各產品設定與操作限制見上方功能文件。
