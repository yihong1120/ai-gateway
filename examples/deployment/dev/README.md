# A 的 AI dev 環境與 B 的 HTTPS 入口

`https://ai-dev.changdar-server.mooo.com` → B Nginx → Tailscale → A dev Open WebUI。
正式 AI 網站仍使用 B 的 Open WebUI／LiteLLM。此設定不啟用 `dev.changdar-server.mooo.com`。

公司主網站 dev 的獨立 BFF 現可透過 `/bff/ai_access/api/company-ai` 使用本服務。
`python3 examples/deployment/dev/connect_bff.py` 僅配置 A 上兩套 dev 的私有設定並保留備份；套用後重啟
`ai-gateway-dev-ai-access` 與 `construction-dev-management`。公司 dev gateway／BFF 本身仍由
`Construction-Hazard-Detection` repo 管理。BFF 使用固定 loopback 18083、dev audience，
不持有 LiteLLM master key 或 company-ai bridge key。Windows 與 ai-dev 既有入口不變。

## 隔離範圍

| 項目 | dev |
| --- | --- |
| 開發原始碼 | A `/home/changdar/Documents/ai-gateway` |
| Open WebUI | A `127.0.0.1:18082` |
| LiteLLM | A `127.0.0.1:14002` |
| PostgreSQL | 專用 Compose 容器，A `127.0.0.1:15432` |
| 資料庫與角色 | `openwebui_dev`、`litellm_dev`，無正式資料副本 |
| 檔案、任務與上傳 | `~/.local/share/ai-gateway-dev` |
| 私密設定與金鑰 | `~/.config/ai-gateway-dev`，不納入 Git |
| OIDC | 共用 Keycloak realm，獨立 client `open-webui-dev` |
| 模型 | 既有雲端模型及 A 的 Gemma 4／OCR；Gemma 走 `127.0.0.1:5003`，不另載入 GPU 模型 |

目前只有 ChangDar 擁有 dev client 的 `dev-user`／`dev-admin` 角色。dev 不可透過共用帳號管理 API 修改正式 Keycloak 帳號。新增測試者時只授予此 client 的 `dev-user`。
dev 啟動時會補齊「帳號與連線」、「EIP 公文工作台」、「公司工作助理」三個 INFO 導覽提示，並保留其他公告；帳號提示明確標示 dev 不提供共用 Keycloak 帳號管理。
dev LiteLLM 全站最多 2 個並行請求；公文模型最多 2、OCR 最多 1、公司助理任務最多 1。
dev 的每使用者每日雲端額度為 US$1；聊天與文件服務各有獨立 US$1/day 金鑰上限。額度不會寫入正式 LiteLLM。
OCR 上限只限制 dev 本身；正式 OCR 服務與 GPU 仍共用，測試大型批次時須觀察總負載。
EIP／NAS 綁定與公司助理權限不複製；若在 dev 手動綁定真實帳號，外部系統仍是真實系統。
company_agent 使用 dev 的一般使用者 LiteLLM 位址與金鑰；啟動時只在沒有加密資料的情況下建立獨立 `checkpoint.key`／`nas.key`。已有加密資料但遺失金鑰時會停止並要求還原，不自動換新金鑰。

## 初次啟動（A，非 sudo）

```bash
python3 /home/changdar/Documents/ai-gateway/examples/deployment/dev/setup.py
```

需已有 A 的 `webui_env`／`llm_env` 與 Docker。LiteLLM 使用獨立 dev venv；既有 `setup.py`
集中讀取原生 YAML、合併 dev 政策、投影模型環境，以及執行固定版本 Prisma 相容處理與 client 生成。
`examples/litellm/profiles/dev.yaml` 只保存原生政策區塊，模型清單沿用主 YAML，生成設定與 Prisma client 僅寫入 dev 環境。
WebUI 使用既有 `webui_env` 的套件。修改應用程式後重啟 dev；升級套件前應另外建立獨立 WebUI venv。
腳本可重跑，保留 dev 的資料與金鑰；Prisma schema 同步不使用 `--accept-data-loss`。
初次配置 OIDC 需讀取 A 既有的私密 Keycloak 管理員設定，該密碼不寫入 dev 服務設定。

手工服務設定來源只有根目錄 `config.local.toml`。setup 依此設定及獨立 dev 的生成憑證，
產生 `~/.config/ai-gateway-dev` 下的 JSON 快照與 `postgres.env`；後者只將既有資料庫憑證
投影給 PostgreSQL 容器，不是另一份要手工維護或 `source` 的環境設定。
生成快照、資料庫憑證與金鑰都是私密部署狀態，不納入 Git；重跑 setup 保留既有 dev 憑證。

## 公開入口（B，需要 sudo）

```bash
sudo python3 "$HOME/ai-gateway-dev-gateway/deploy.py"
```

此部署檔由 `gateway.py` 複製而來。先檢查 A dev 的私有健康端點，再新增單獨 Nginx site 與 ai-dev 憑證，沿用 B 既有 ACME 帳號。
每次 reload 前檢查 Nginx 語法；原有三個正式站點保留。DNS／路由器 80、443 仍指向 B。
reload 後最多等待 45 秒，持續驗證正確 dev 憑證、gateway/app 標記與健康回應；不略過 TLS 驗證。
憑證由 B 既有 certbot timer 自動續期；更新後 hook reload Nginx。
已部署時可在 B 用 `python3 "$HOME/ai-gateway-dev-gateway/deploy.py" --verify-only` 再檢查，不需 sudo，也不會 reload 或申請憑證。

## 日常開發（A）

2026-10-27 已將 dev LiteLLM 升級為 `1.106.4`，使用獨立的
`~/.local/share/ai-gateway-dev/venvs/litellm-1.106.4`，全域並行仍為 `2`。
完整版本鎖定、正式部署與回復方式見 [LiteLLM 文件](../../litellm/README.md)。

```bash
systemctl --user restart ai-gateway-dev-litellm ai-gateway-dev-webui
systemctl --user status ai-gateway-dev-webui ai-gateway-dev-litellm ai-gateway-dev-relay
journalctl --user -u ai-gateway-dev-webui -n 80 --no-pager
python3 /home/changdar/Documents/ai-gateway/examples/deployment/dev/verify.py
```

`verify.py` 從已生成的 LiteLLM YAML 取得期望模型清單及全域並行政策，核對服務與資料庫 readiness，
並檢查 OIDC 登入導向；不代表完成瀏覽器 SSO 或取代完整協定／預算驗收。
自訂 DEV 角色檢查須相容目前 Open WebUI 的 `token_claims` 關鍵字參數；只接受 `dev_roles` 中的 `dev-user`／`dev-admin`，userinfo 沒有該欄位時才使用 OAuth manager 已驗證的 token claims。

```bash
python3 -m pytest examples/deployment/dev/test_isolation.py examples/deployment/dev/test_gateway.py examples/deployment/dev/test_state.py -q
~/.conda/envs/webui_env/bin/python examples/deployment/dev/test_oauth_callback.py
```

回呼測試使用已安裝 WebUI 的登入回呼程式，涵蓋 DEV 管理員／一般使用者、ID token claim 補回及拒絕缺少 DEV 角色的帳號。IdP 回應、資料寫入與登入憑證簽發使用測試替身；實際 Keycloak 登入仍需在瀏覽器驗證。2026-10-14 修正舊角色檢查簽名造成的 `unexpected keyword argument 'token_claims'`，共 32 項測試通過。

完整 company_agent 功能驗證可執行 `~/.conda/envs/webui_env/bin/python examples/deployment/dev/smoke_company.py`；會建立一個標示為驗證用的 dev 聊天、實際呼叫模型並確認工作完成及聊天結果已保存，因此會扣 dev 額度。

### 阿醒：本地 Gemma 嘴砲朋友

在模型選單選擇 **阿醒｜戀愛腦嘴砲朋友**（`love_brain_friend`）。它使用既有 Gemma 4 12B，吐槽戀愛腦、拆曖昧藉口，也能陪人打嘴砲；說「今天別嗆」即可換成溫和口吻。模型附四個開場話題，設定為純文字聊天、本地零雲端費用，關閉推理輸出以直接回話。

人設包含身分、性格反差、朋友關係、日常喜好、說話節奏及情境切換。第三版採更直接、沒禮貌的損友口吻，可以自然罵髒話、抓語病、接挑釁；平常約 15～80 字，嘴中就停，只有明確問做法時才給短建議。一般自介先報「阿醒」，問真實身分時如實說 AI，問技術時才說 Gemma，不捏造真人履歷。能聊日常廢話，要求一句就回一句，要代寫訊息就只給正文；真的不想被嗆時說「今天別嗆」。調整後建議開新對話，避免舊回覆繼續影響口吻。

角色定義與提示詞在 [personas/love_brain_friend.json](personas/love_brain_friend.json) 和 [personas/love_brain_friend.prompt.txt](personas/love_brain_friend.prompt.txt)。調整後，用 `~/.conda/envs/webui_env/bin/python examples/deployment/dev/persona.py` 更新 dev；一般提示詞改動不需重啟。LiteLLM 路由與環境快照統一由 `setup.py` 產生，`persona.py` 只驗證既有零費用路由並安裝角色，不修改部署設定。`seed.py` 也會安裝這個角色，重新配置 dev 時保留它。

`persona.py` 會為已確認為 dev operator 的舊帳號補上缺少的 WebUI credential/session 紀錄，密碼隨機產生且不保存為明文；不啟用已停用的帳號，OIDC 限制保持原設定。驗證程式使用帶有目前 session stamp 的短效 automation token，符合 WebUI 的登入撤銷檢查。

```bash
~/.conda/envs/webui_env/bin/python examples/deployment/dev/smoke_friend.py --output docs/love-brain-friend-dev-v3-validation-2026-10-14.json
```

此驗證直接走 dev WebUI，確認已保存最新提示詞，再測十四個情境：自介、個性、真實 AI／模型身分、戀愛腦吐槽、加辣吐槽、不對等付出、多輪單句互嘴、客服口吻反饋、日常聊天、不過度解讀、安慰、延續傾聽和代寫訊息。輸出包含合成對話及 token usage；自動檢查只能驗證部分規則，幽默與自然度仍需看實際回話。保留[第一版](../../../docs/love-brain-friend-dev-validation-2026-10-14.json)和[第二版](../../../docs/love-brain-friend-dev-v2-validation-2026-10-14.json)作為調整前參考，最新結果在[第三版紀錄](../../../docs/love-brain-friend-dev-v3-validation-2026-10-14.json)。

修改 Function 原始碼需另執行 `webui_env/bin/python examples/deployment/dev/seed.py` 更新 dev 儲存的 Function，再重啟 dev WebUI；不修改正式 Function。

Gemini 4 Argon 的完整接入驗證：

```bash
~/.conda/envs/webui_env/bin/python -m examples.deployment.diagnostics.gemini4_smoke --output docs/gemini4-dev-validation-2026-10-14.json
```

此測試涵蓋 LiteLLM／WebUI 模型清單、聊天、串流、三種推理等級、工具呼叫往返、JSON schema 與費用落庫，會扣 dev 額度。dev LiteLLM 重啟時，因 WebUI 的 systemd `Requires` 相依性可能一併停止 WebUI；待 LiteLLM 健康後，用 `systemctl --user start ai-gateway-dev-webui ai-gateway-dev-relay` 恢復入口。

Git commit／push 只保存原始碼；正式更新應經 dev 驗證後另行部署，不能直接同步 dev 的資料庫或私密設定。
全部 dev user units 已 enable，A 的 linger 需保持啟用。A 關機時 dev 與共用 OCR 暫停，B 的正式 CPU 服務繼續運行。

### Windows Claude Code 自助設定

獨立的 `ai-gateway-dev-ai-access` 服務在 A 的 `127.0.0.1:18083` 處理電腦綁定、受限 virtual key、自動續期與撤銷，經既有 ai-dev 入口提供服務。WebUI 只持有 loopback bridge 憑證，不持有管理服務的 LiteLLM master key。初次配置使用 `examples/deployment/dev/setup_ai_access.py`；詳見 [後端與部署設計](../../company_ai_access/README.md)。

Flutter 在另一台電腦的 repo 開發，請交付 [Flutter 工程師串接指引](../../../docs/company-ai-access-flutter-handoff.md) 與 [OpenAPI](../../../docs/company-ai-access-openapi.json)。預設核准頁是 `/company-ai/setup`，另預留 `/company-ai/portal/` 給 Flutter build。此功能保留現有 Keycloak-subject 額度機制，CLI 與 WebUI 共用 dev US$1/day；正式環境尚未部署。

啟用後日常檢查及停止 dev 時，另將 `ai-gateway-dev-ai-access` 納入服務清單。私密設定與 registry 不可同步到正式環境。Windows 真正的 DPAPI、瀏覽器確認和 Claude Code 啟動須依交接指引在 Windows 驗收。

停止 dev（保留資料）：

```bash
systemctl --user stop ai-gateway-dev-relay ai-gateway-dev-webui ai-gateway-dev-litellm ai-gateway-dev-db
```
