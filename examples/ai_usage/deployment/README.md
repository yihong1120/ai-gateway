# AI 用量歷史分類修正

`reclassify.py` 提供 prepare、apply、rollback 三個步驟。需在正式 B 的私有
環境執行，使用包含 psycopg、LangGraph 加密 serializer 的 WebUI Python。
只改 LiteLLM_SpendLogs.metadata，不改 end_user、金額、token、模型與時間。
此操作不會建立 OIDC 連結，也不會替缺乏證據的公文指定使用者。

## 證據與分類

- company_agent：加密 checkpoint 中 AIMessage.response_metadata.id 精確等於
  帳務 request_id；checkpoint 的 billing_identity.owner 必須與伺服器 jobs
  owner 相同，有 web_jobs 時也必須一致。subject 必須已連結目前 tenant 的帳號，
  且等於帳務既有 end_user。任何衝突都跳過並報告。
- official_document：使用設定中釘選的專用公文 key，不從相近時間或檔名推測。
- 未歸戶類別：依 call_type、專用公文 key、既有身分及 status 重新分類。
  具備其他 tenant 身分的紀錄排除，不可重新歸入本 tenant。

prepare 只讀取完成至少五分鐘的紀錄，建立私有 plan、原始 metadata、證據與
每筆計費欄位雜湊。目錄權限 0700、檔案 0600，不能放進 Git。一般報告僅有
彙總數字。計畫會綁定主機、資料庫與檔案雜湊；apply 在單一交易內鎖定目標列，
發現任一筆計費欄位或 metadata 已變更就整批回滾。提交前逐筆再驗證。

## 執行

以下以 repo 根目錄執行；`--root` 指向具有私人 `config.local.toml` 的正式 checkout。
工具分別讀取 `[ai_usage]`、`[litellm]`、`[webui]` 的設定，不合併各程序身份或金鑰。
帳號目錄由 `[ai_usage]` 的 `AI_USAGE_ADMIN_DATABASE_URL` 明確指定，並以唯讀交易查詢；
工具不依賴其他 repo 或歷史遷移目錄的私人快照。WebUI／LiteLLM 設定從同一次讀取取得，
寫回收條保留為私有操作證據。先驗證來源、測試與 dry run 彙總，再套用該計畫。

```bash
ai-gateway exec ai_usage -- python -m examples.ai_usage.deployment.reclassify prepare \
  --run-dir /private/usage-reclassification-UNIQUE

ai-gateway exec ai_usage -- python -m examples.ai_usage.deployment.reclassify apply \
  --run-dir /private/usage-reclassification-UNIQUE
```

需要還原時，將同一指令的 apply 改成 rollback，使用相同 run-dir。rollback
必須已成功 apply，且每筆 metadata 仍等於修正後版本；遇到後續變更就拒絕覆寫。
每個動作完成後寫入收據，不允許同一計畫重複執行。API 程式還原與 metadata
還原分開處理；1.4.0 仍能讀取修正後的 company_agent 來源標記。

本次 30 天比較窗固定為 2026-09-29～2026-10-28（Asia/Taipei）；它只用於
報告中的比較數字，實際 prepare 掃描所有留存且已完成的歷史紀錄。

## 原生 App／瀏覽器用量路由

AI 用量路由工具的唯一來源已從工安 repo 的
`deploy/ai_usage_mobile_route/deploy.py` 移至本 repo 的
`examples/ai_usage/deployment/mobile_route.py`。來源搬移保留原有主機、Nginx
設定檔及備份目錄，不會自動部署或修改線上的設定。

這支工具針對既有 `phase6-main.conf` 的 Cookie-only BFF 路由進行一次性切換。
原生 App 的 Bearer 請求轉至 AI Usage API `127.0.0.1:8012`；瀏覽器請求仍
透過 BFF `127.0.0.1:8006` 驗證 session cookie。公開路徑、Bearer 與 Cookie
轉送契約不變，兩個後端分別驗證自己的憑證。

在目標主機的來源 checkout 根目錄，先明確執行檢查：

```bash
python -m examples.ai_usage.deployment.mobile_route --check
```

`--check` 會讀取實際 Nginx 設定，並以 curl 對該主機 loopback 上的三個 HTTPS
網站及 Web／原生用量路由進行真實網路探測；它不寫檔、不備份、不 reload。
這不是純離線驗證，也不驗證真實使用者的用量；探測使用無憑證及刻意無效的 Bearer。
已存在新路由或 map 時會要求人工檢視，不重複套用。

只有操作者明確決定套用時，才在目標主機以適當權限執行不帶 `--check` 的命令：

```bash
sudo python3 -m examples.ai_usage.deployment.mobile_route
```

套用會備份原站台設定、寫入 Nginx map 與 location，通過 `nginx -t` 後 reload，
再核對原生與瀏覽器認證路由及三個網站。若套用或探測失敗，還原站台設定、移除本次
map，重新檢查並 reload。它不修改模型、AI 帳務、資料庫或使用者授權。
