# Open WebUI 0.12.4 升級紀錄

此為 2026-10-27 的歷史驗證與備份紀錄；B 機專用的一次性升級／探測腳本已移除。

2026-10-27（Asia/Taipei）已驗證並將 B 的 Open WebUI 從 `0.12.3` 升級到 `0.12.4`，與 A 一致，沿用 B 的 conda `webui_env`、
`openwebui.service`、公司 launcher 與設定。LiteLLM 使用自己的獨立執行環境，本次不變更其版本與並行上限。

## 驗證與相依套件

先使用 B 的獨立 PostgreSQL instance 與 DATA_DIR 複本，檢查管理員／員工模型權限、SSO 導向、
公文、公司助理、連線頁、工作佇列、檔案上傳讀回與刪除，再送出少量真實聊天／串流請求。
原有 user、chat、file、function、model、config 資料以選定欄位雜湊比對；回復演練也核對相同雜湊。
256 項相關回歸與 6 項原生 SSO 回呼測試通過；B 的獨立副本通過 21 項檢查，正式服務通過 18 項檢查。
正式聊天的 response ID 也已對應到 LiteLLM 費用紀錄，確認歸屬登入員工，無需開啟對話內容儲存。
SSO 回呼使用新版的實際程式與模擬 IdP 回應驗證；未自動完成真人瀏覽器登入。

驗證用 automation token 有 session stamp 且短效。`/api/v1/auths/` 是真人 session 專用端點，
對 automation token 回傳 `403` 是預期保護；模型與聊天 API 另行驗證成功。

新版需要 MCP／MCP Types `2.3.0`、Pillow `13.0.0`、pypdf `6.21.2`、uvicorn `0.54.0` 等更新。
完整 15 項套件變更與新舊離線 wheel 的 SHA256 manifest 存放於 B 的私密部署目錄：
`/home/changdar02/.local/state/ai-gateway/webui-release-0.12.4-20261027`。
公文、Office 與上傳測試通過後，專案 Pillow 相容範圍更新為 `>=11.1,<14`，並刷新 editable metadata。
WebUI 環境原有的舊 LiteLLM SDK／boto3 相依警告保留，本次不新增相依衝突；正式 LiteLLM 服務使用獨立的 `1.106.4` 環境。

此次 Open WebUI 兩版的 Alembic migration 檔案完全相同，資料庫 head 保持 `0bd252b206fb`。
既有個人 Direct Integrations 設定保持原值；預檢確認沒有員工保存的個人 tool server。
新版 Manage Files 列表只顯示目前使用者自己的檔案，包含管理員；既有資料不會因列表範圍變更而刪除。

## 備份與部署

B 備份目錄：`/var/backups/openwebui/20261026T182616Z`（目錄時間為 UTC；操作日期為 2026-10-27 Asia/Taipei）。
約 21 GiB 的 DATA_DIR 先在服務運行時複製；停止服務後再同步最後差異，並取得完整資料庫與 public schema dump。
dump 必須通過 `pg_restore --list`，再安裝已核對 SHA256 的離線套件。
備份也包含環境、原 service、launcher 與 pyproject。WEBUI_SECRET_KEY、帳號、聊天、檔案及加密綁定沿用原資料。

當時的部署程式僅適用於已通過預檢的 B，要求管理員權限與閒置工作佇列。
安裝失敗時會恢復舊套件與 pyproject，重啟 `0.12.3`；由於 migration 未變，不自動倒灌資料庫，避免丟失新用量／訊息。
當時的回復材料保存在備份與 stage 的 `rollback-wheels`、`package-changes.json`；獨立副本的回復探測不作為正式資料庫還原工具。

驗證結果：[openwebui-upgrade-0.12.4-2026-10-27.json](openwebui-upgrade-0.12.4-2026-10-27.json)。

官方資料：[v0.12.4 發佈說明](https://github.com/open-webui/open-webui/releases/tag/v0.12.4)、
[更新與備份指南](https://docs.openwebui.com/getting-started/updating/)。
