# 開發與貢獻

本文件集中安裝、測試及發布方式。服務用途與啟動模組見 [功能索引](examples/README.md)，
應用資源與資料契約見 [架構說明](docs/runtime-refactor.md)。

## 環境與依賴

應用程式需要 Python 3.11 以上，LiteLLM server 需要 Python 3.12 以上。
使用根目錄 `pyproject.toml` 與 `uv.lock`。所有 Python runtime 在這裡宣告及鎖定，
不另維護功能目錄的 requirements 或私人套件清單。依角色選擇安裝組合；部署環境集中在根目錄 TOML，產品目錄只維護模型政策與業務設定。

| 安裝組合 | 責任 |
| --- | --- |
| extra `identity-services` | 認證、BFF 與管理 API |
| extra `company-ai-access` | 裝置註冊、金鑰交付與 CLI 代理 |
| extra `ai-usage` | 個人／管理用量 API |
| extra `documents` | 公文解析與輸出 |
| extra `company-agent` | 公司助理、檔案工具與 LangGraph |
| group `dev` | 測試、靜態檢查與建包工具；含評測價格表所需的 LiteLLM SDK，預設啟用 |
| group `litellm` | 獨立 LiteLLM server runtime，Python 3.12 以上 |
| group `office-worker`／`cad-worker` | 各自隔離的工作容器依賴，Python 3.11 以上 |

安裝 uv 0.9.9（與 CI／建置工具一致），在 repo 根目錄以 `--all-extras` 安裝全部應用及預設 dev 組：

```bash
python -m pip install uv==0.9.9
uv sync --frozen --all-extras
source .venv/bin/activate
```

`source .venv/bin/activate` 適用 Linux 的 bash／zsh；`uv sync` 不會自動啟用 PATH。
啟用後可直接使用 `ai-gateway`，也可透過相同組合的 `uv run` 執行。

只安裝單一應用時關閉 dev 組，例如：

```bash
uv sync --frozen --no-dev --extra company-ai-access
```

執行時沿用相同 extras／groups，避免 uv 將環境同步回不同組合。
LiteLLM 與 worker 使用獨立環境，`--only-group` 不安裝本專案：

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.local/share/ai-gateway/venvs/litellm" uv sync --frozen --only-group litellm --python 3.12
```

其他 worker 對應改用 `office-worker` 或 `cad-worker`，且各用不同環境目錄。
不要以 `--all-groups` 混裝 server／worker。Open WebUI 仍由既有宿主環境安裝；
`--all-extras` 只涵蓋本 repo 的應用，不能重建 WebUI 宿主或 GPU driver／模型權重。
正常產品依賴不含 LiteLLM SDK；dev 組僅為評測價格表使用 SDK，不安裝 proxy server。

每個 runtime 使用自己的虛擬環境；共用同一份鎖不代表要把模型代理、WebUI 與管理 API
全部安裝在同一個程序或使用相同資料庫。實際 Python 版本範圍以對應安裝組合為準。
`uv run --frozen` 沿用已提交的鎖；調整依賴時修改 pyproject，再用 uv 更新並提交鎖。

## 環境設定

```bash
cp -n config.example.toml config.local.toml
chmod 600 config.local.toml
# 填入實際設定後：
ai-gateway check
```

公開範本是 `config.example.toml`，私人來源只有已忽略的 `config.local.toml`。
`webui`、`litellm`、`local_inference`、`company_ai_access`、`ai_usage` 與可選的 `identity`
各有自己的區塊，
沿用現有環境變數名稱；程序只取得所選區塊。程序既有環境變數優先於檔案，
各服務的資料庫、管理金鑰、受限 key 與身份權威不因放進同一檔案而共用。
CSV／JSON 設定仍保持字串，選填註解不會自動啟用。

應用 factory 載入自己的區塊；操作工具及容器透過同一個 CLI 注入設定：

```bash
ai-gateway compose litellm up -d
ai-gateway --config /private/config.toml check
ai-gateway --config /private/config.toml exec webui -- python -m examples.openwebui.serve
```

`--config` 是全域參數，放在 `exec`／`check` 前。CLI 安裝於應用環境；尚未啟用該環境時，
在命令前使用相同安裝組合的 `uv run --frozen ...`。
原生 executable／參數可使用加引號的 `'{LITELLM_EXECUTABLE}'` 等佔位符，
由 CLI 從所選區塊替換，避免 shell 提前展開。

## Docker 入口

全 repo 使用根目錄一份 `Dockerfile`、`docker-compose.yml` 與 `.dockerignore`。
Compose 透過 profile 選服務；Dockerfile 透過 target 選建置環境，保留 API、GPU、Office
與 CAD 所需的依賴及隔離。工作容器仍由原本的受限 `docker run` 逐次建立，不共用工作卷。

```bash
ai-gateway compose litellm up -d
ai-gateway compose local-model up -d
ai-gateway compose ocr up -d
ai-gateway compose images build
# 可選的私人靜態檔案服務：
ai-gateway compose files up -d
# 同一模型部署多個實例：
ai-gateway compose --project another-model local-model up -d
```

`compose` 從 repo 根目錄執行，只載入所選 profile 對應的 TOML 區塊；啟動前確認必要設定，
不自動讀取 `.env`，也不將其他服務的金鑰投影到容器。`images build` 不讀私人服務設定。
單一 target 可用 `docker build --target eip-office -t ai-gateway/eip-office:1 .`。
開發資料庫仍由 dev setup 管理，使用固定的 `ai-gateway-dev` project 及既有獨立 volume；
完整 profile／target 對照見 [部署說明](examples/deployment/README.md)。

既有環境可明確執行 `ai-gateway migrate` 一次轉存舊設定。它拒絕覆寫目標，
保留原檔作為未部署的相容備份；不會部署或重啟服務。此後只維護中央 TOML，
不自動並讀舊 env。開發／正式環境的 JSON 快照及服務收條是部署產物，
不作為另一套手工環境來源；私密快照同樣不提交 Git。

`~/.config/ai-gateway-dev/postgres.env` 是 dev setup 從已保存的獨立 dev 資料庫憑證
產生的容器投影，只包含 PostgreSQL 啟動所需的兩個值，不另行手工維護。
它與 JSON 快照、金鑰狀態及服務收條同屬私密部署產物；手工設定來源仍只有中央 TOML。
歷史封存中的 `.env.example` 保留原始位元組供完整性核對，不作為目前設定範本。

應用從 `examples.shared.config` 匯入 `read_service`；需要同時處理多個服務的工具，
使用同模組的 `read_services('webui', 'litellm', path=...)` 一次讀取及驗證，再取得指定區塊，
避免設定檔替換時混用不同版本。此模組只處理 TOML 設定，不包含 Docker 或轉存操作。
啟動管理由 `examples.deployment.cli` 提供；`ai-gateway` 與
`python -m examples.deployment.cli` 使用相同入口，`exec` 的驗證與投影沿用同一次讀取。

本機 vLLM 的部署環境屬 `[local_inference]`，只含映像、模型目錄、YAML 路徑、GPU 與埠。
模型選項使用根目錄私人 `vllm.local.yaml`，由公開的 vLLM YAML 範本建立，直接交給後端讀取；
不在應用層重建一套模型參數映射。LiteLLM gateway 的模型連線仍留在 `[litellm]`。
詳見 [本機推論](examples/litellm/README.md#本機推論)。

## 離線驗證

需要 PostgreSQL server tools（initdb／pg_ctl）、Redis server、Node.js 與 uv，
以一般使用者執行；容器或原生 CLI 測試需要相應工具。

```bash
uv run --frozen --all-extras python -m examples.deployment.tools.validate
```

可加 `--output /tmp/ai-gateway-validation-UNIQUE` 指定尚未存在的報告目錄。

驗證器建立私人暫存 PostgreSQL／Redis，覆寫測試 DSN，結束後停止並清除測試資料。
它涵蓋靜態檢查、設定產物、鎖、兩組 unittest、pytest／原生 CLI、乾淨 sdist／wheel、
repo 外匯入及服務發布包。report.json 分別列出通過、失敗與略過項目。
歷史封存的位元組核對需明確加 --with-archive 或 --archive-dir；參數見
[部署驗證](examples/deployment/README.md)。

針對修改可先執行相應測試模組；需資料庫的案例應使用隔離測試資源。
真實模型、EIP、NAS、郵件或供應商評測是另外的明確操作，不作為一般回歸的一部分。

## 修改準則

- 每項功能保留單一來源；應用只負責組裝，服務建立時持有依賴。
- 資料在入口驗證；鎖、容量規則與資料變更放在同一 service 交易，成功成果由明確結果傳回。
- 生成的發布包與驗收輸出放在已忽略的 `artifacts/`、`test_outputs/`，不寫回來源目錄。
- 保留取消、交易、owner 範圍、部分成功及結果不明不重送的契約。
- 私密環境、金鑰、輸入文件、資料庫、產物與本機封存不加入版本控制；提供可公開的範本。
- 不在產品 README 重複安裝依賴；在產品文件說明設定、入口與業務限制。
- 認證修改依 [AGENTS.md](AGENTS.md) 檢查相鄰後端及 Flutter，沿用各登入模式的身份權威。
- 本 repo 測試、其他 repo 原有變更及私人資料分開處理；不以整理名義覆蓋它們。

## 發布

```bash
uv run --frozen --all-extras python -m build
```

調整依賴後執行 `uv lock`，以 `uv lock --check` 檢查 pyproject 與鎖一致。
依賴新增到相應的 extra／group，不另新增 requirements 檔；變更版本需驗證對應 runtime。

正式 wheel 保留執行套件與資源，測試及主機專屬部署工具留在 checkout。
完整驗證從新 sdist 建立 wheel 並在 repo 外檢查匯入，避免本機舊 build 內容混入。
BFF 與公司 AI 的來源包由各自產生器建立並核對雜湊，見 [部署工具](examples/deployment/README.md)。
版本更新另依產品程序排空工作、備份、安裝與驗證；跑測試或建包不會自動部署。
