# 共用部署工具

部署註冊 API 位於 [registry/](registry/README.md)，其他服務專屬工具放在各服務的 `deployment/`；這裡集中跨服務的基礎設定、診斷、開發環境與封存資料。
所有操作工具從 repo 根目錄執行，沿用原有環境隔離、權限與部署前置檢查。
`examples.deployment.cli` 提供 `ai-gateway check`、`exec`、`compose`、`migrate`；
也可用 `python -m examples.deployment.cli` 執行相同入口。
應用使用的純 TOML 讀取與投影 API 位於 `examples.shared.config`，不依賴 Docker 或轉存工具。

| 功能 | 目前位置 |
|---|---|
| 部署註冊、註冊碼兌換與簽署 | `registry/`（由管理應用掛載） |
| 公司 AI 正式部署、升級與驗證 | `examples/company_ai_access/deployment/production/` |
| 公司 AI Windows 建置、憑證與簽署 | `examples/company_ai_access/deployment/windows/` |
| BFF 單一來源部署產物 | `examples/bff/deployment.py` |
| 用量資料修復／還原 | `examples/ai_usage/deployment/` |
| LiteLLM 原生 YAML、版本鎖與部署設定 | [LiteLLM](../litellm/README.md)；dev 初始化在 `dev/setup.py` |
| WebUI 啟動與 Function 入口 | `examples/openwebui/` |
| 獨立公司資源 MCP | `company_mcp/`；`python -m company_mcp.serve` |
| Nginx、systemd、PostgreSQL 基礎設定 | `nginx/`、`systemd/`、`postgres/` |
| 跨服務效能與成果診斷 | `diagnostics/` |
| Docker 建置、服務 profiles 與 context 白名單 | 根目錄 `Dockerfile`、`docker-compose.yml`、`.dockerignore` |
| 公文 OCR 原生設定與 PaddleX API | `examples/official_document_workflow/deployment/ocr/` |
| 共用開發環境 | `examples/deployment/dev/` |
| 工作區驗證與封存核對 | `tools/` |
| 舊驗證快照與發布檔 | `examples/deployment/archive/` |

原頂層 `deploy/` 已移除，沒有第二套相容性入口。私人設定、state 目錄與正式機上的
既有發布位置維持原有約定。服務入口與這些 source-checkout 部署工具分開管理；
wheel 不包含歷史發布包、開發快照或服務的部署 staging 內容。

## Docker 入口

從 repo 根目錄操作。各 profile 由 CLI 讀取自己的中央設定區塊，再交給同一份
`docker-compose.yml`；不把整份私人 TOML 掛入容器，也不載入舊 `.env`。

| Profile | 用途／設定來源 |
| --- | --- |
| `litellm` | LiteLLM proxy；`[litellm]` |
| `local-model` | 通用 vLLM；`[local_inference]` 與私人原生 YAML |
| `ocr` | OCR vLLM 後端；`[ocr]` 與私人原生 YAML，PaddleX API 仍獨立執行 |
| `files` | 明確啟用的檔案服務；`[fileserver]` |
| `dev-db` | 隔離 dev PostgreSQL，由 dev setup 管理，project 固定為 `ai-gateway-dev` |
| `images` | 建置工作映像，無需服務憑證 |

```bash
ai-gateway compose litellm up -d
ai-gateway compose local-model up -d
ai-gateway compose ocr up -d
ai-gateway compose images build
```

根目錄 Dockerfile 的 targets 為 `management-api`、`company-office`、`eip-office`、
`company-cad`、`eval-preview`，每個 target 只安裝自己的 runtime。
也可單獨建置，例如：

```bash
docker build --target eip-office -t ai-gateway/eip-office:1 .
docker build --target company-office -t ai-gateway/company-office:1 .
docker build --target company-cad -t ai-gateway/company-cad:1 .
```

Office／CAD 仍由工作流程逐次啟動臨時容器，掛載該次私人輸入與輸出，保留無網路、
唯讀根檔案系統、權限及資源限制；不改為常駐、共用資料卷的工作服務。
需要多個實例時以 `ai-gateway compose --project 名稱 PROFILE ...` 指定 project。
搬移來源不會重新建立現有容器或切換主機服務；既有部署須核對原 project、資料卷及設定後另行切換。

## Systemd 與主機程序

容器統一由根目錄 Compose 管理；不同宿主程序的範本集中在 `systemd/`：

| 範本 | 程序與範圍 |
| --- | --- |
| [openwebui.service.example](systemd/openwebui.service.example) | Open WebUI 與應用組裝，user service |
| [paddleocr-vl16-api.service.example](systemd/paddleocr-vl16-api.service.example) | PaddleX `/layout-parsing` API，user service；等待 OCR vLLM 後端 |
| [ai-gateway-ai-usage.service.example](systemd/ai-gateway-ai-usage.service.example) | 唯讀用量 API，user service |
| [company-ai-access.service.example](systemd/company-ai-access.service.example) | 公司 AI 裝置授權與代理，system service |
| [company-mcp.service.example](systemd/company-mcp.service.example) | 獨立 MCP ASGI程序，最小依賴／專用使用者／設定，system service |

部署時調整各自的 Python、工作目錄與可寫範圍，依 user／system service 的管理範圍安裝，
檔名移除 `.example`。WebUI 直接使用 canonical 啟動器，無需另一份 EIP `ExecStart` drop-in。
LiteLLM 原生模式保留[前景命令](../litellm/README.md#啟動)；私有推論入口使用既有 Nginx 的
[server include 片段](nginx/local-inference-upstream.conf.example)，不另維護 proxy service。

dev 工具仍依隔離的設定及狀態產生自己的 units；CRL 安裝工具仍產生定期更新的 service／timer。
它們的資料隔離、生命週期及簽署契約維持原樣。這些 repo 範本與生成工具不代表目前主機已安裝的狀態，
來源整理不會修改、停用或重啟現有 host 服務。

## 常用命令

```bash
python -m examples.bff.deployment artifacts/identity-services
python -m examples.deployment.dev.run --help
python -m examples.company_ai_access.cli.release --help
python -m examples.deployment.tools.verify_archive
```

開發環境完整操作見 [dev README](dev/README.md)，各產品的正式操作見對應部署目錄。
本機 systemd user unit 的 `ExecStart` 若指向舊的 `deploy/dev/run.py`，請更新為
`examples/deployment/dev/run.py`，保留原本 Python、WorkingDirectory 與服務參數。

## 封存

`archive/dev` 保留一次性驗證快照；`archive/releases` 保留原發布包、簽章與雜湊檔。
這兩個目錄屬本機封存，已加入 `.gitignore`；版本控制只保留封存說明及 `archive/integrity.json`。
內容以原始位元組搬移，不改寫內嵌路徑、manifest 或簽章。歷史證據 JSON 中的舊路徑
描述當時執行位置；查找檔案時依此目錄對照。`archive/integrity.json` 記錄搬移時的
SHA-256，使用上述驗證命令可檢查完整性。封存副本不作為目前的執行來源。

乾淨 checkout 不需要封存檔。一般完整驗證會檢查 manifest 格式與路徑，將封存內容核對
標記為 skipped，不宣稱已核對不存在的檔案。需要歷史稽核時，從原發布附件或既有離線備份
取得檔案，保留 `dev/`、`releases/` 的目錄結構；目前沒有設定自動下載來源。

```bash
# 只檢查受版本控制的清單，不讀取本機封存內容
python -m examples.deployment.tools.verify_archive --manifest-only
# 嚴格核對本機封存；任何缺檔或雜湊不符都會失敗
python -m examples.deployment.tools.verify_archive
# 核對還原到 repo 外的封存，清單仍以本 repo 的 integrity.json 為準
python -m examples.deployment.tools.verify_archive --archive-dir /private/ai-gateway-archive
# 在完整驗證中要求封存核對
python -m examples.deployment.tools.validate --with-archive
python -m examples.deployment.tools.validate --archive-dir /private/ai-gateway-archive
```

詳細封存取得與目錄格式見 [archive README](archive/README.md)。


## 一次完成離線驗證

安裝組合及統一命令見 [開發指南](../../CONTRIBUTING.md#離線驗證)。
CI 與本機使用同一個 `examples.deployment.tools.validate` 入口。

驗證器使用目前 Python 環境，需要 PostgreSQL server tools、Redis server、Node.js 與 `uv`；請以一般
使用者執行。它自行建立、停止並清除暫存 PostgreSQL cluster，覆寫子程序的測試 DSN，
不使用服務的私人設定或資料庫。舊 Word MCP 整合與手動腳本已移除。
原生 Codex CLI 已安裝時會使用本機假上游驗證完整工具回合；平台或工具缺少造成的
略過會分開列在報告中，不會當成通過。

檢查包含 Ruff、產生檔與 lock、封存清單、兩組完整 unittest、pytest／原生 CLI、
乾淨 sdist→wheel、repo 外匯入，以及公司 AI 與完整身分／BFF 發布包。
預設在私人暫存目錄保留 `report.json`、各階段 log 與產物；可用 `--output NEW_DIRECTORY`
指定報告目錄。CI 使用相同命令，並保存驗證證據；封存位元組的 SHA-256 核對需另加
`--with-archive` 或 `--archive-dir`，預設報告會明確標記此項為 skipped。

Redis 限流測試另啟動只使用私人 Unix socket 的暫存 Redis process，測試後自行停止，
不使用或停止系統 Redis。用量與 PostgreSQL 測試也不存取正式帳務資料。

## 服務與操作工具的發佈範圍

`examples.deployment.registry` 是執行中的 API 服務，維持 `/deployment-registry` HTTP 路徑、
設定名稱、資料表及簽署契約。原本並列的註冊服務目錄已整合，不保留舊命名空間轉接。
Python wheel 與身分／BFF bundle 只包含 `registry/` 及父套件入口；其餘部署、診斷、
開發環境及封存工具只從工作區明確執行，不隨服務載入。

已完成的 2026-10-26 A／B LAN 一次性遷移腳本已移除；歷史驗證與備份位置見
[LAN 遷移紀錄](../../docs/lan-nginx-transport-2026-10-26.md)。

目前只保留 Nginx／systemd 的持續維護設定與修復工具。首次啟用 TLS 及移除舊 8443
listener 的主機專屬腳本已移除；新的部署依目標主機準備憑證、安裝 vhost 並驗證語法。
現行 vhost、舊 URL redirect 範本及續證 reload hook 仍保留，不會修改主機已安裝的設定。

公司 MCP的公開路徑由獨立程序承接；WebUI僅保留私有資源authority與本人確認頁。
[Nginx範本](nginx/company-mcp.conf.example) 包含公開路徑轉送與私有端點封鎖。
MCP程序不持有 DB／vault／IdP client secret；部署與健康檢查見
[公司 MCP設計](../../docs/company-ai-codex-design.md)。
