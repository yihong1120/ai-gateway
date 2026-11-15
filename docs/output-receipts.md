# 公文輸出收條核對

一般 PDF／Word／CSV 與待確認提醒都保存輸出收條。待確認對話到期後，管理員仍可依 owner
與輸出 ID 查詢。工具使用既有 PostgreSQL、schema 與加密金鑰，不建立替代金鑰、
不自行升級資料庫。先由文件服務完成 migration v9，
再於相同服務環境執行；設定沿用 `EIP_DATABASE_URL`（或 `DATABASE_URL`）、
`EIP_DB_SCHEMA` 與 `EIP_AUTOMATION_KEY_FILE`。

```bash
python -m examples.official_document_workflow.cli.output_receipts list --owner USER_ID --state unknown
python -m examples.official_document_workflow.cli.output_receipts inspect --owner USER_ID --output-id OUTPUT_ID
```

`list` 預設最多 50 筆，`--limit` 可設 1–100。`inspect` 包含檔名、來源與人工核對紀錄，
輸出應依服務資料存取規範保存。owner 必須與原始收條完全相符。

將有效的 WebUI 管理員 bearer token 存入私人檔案（權限 0600），使用本機 WebUI origin：

```bash
python -m examples.official_document_workflow.cli.output_receipts verify \
  --owner USER_ID --output-id OUTPUT_ID \
  --webui-url http://127.0.0.1:8080 --token-file /run/user/1000/webui-admin.token
python -m examples.official_document_workflow.cli.output_receipts reconcile \
  --owner USER_ID --output-id OUTPUT_ID \
  --webui-url http://127.0.0.1:8080 --token-file /run/user/1000/webui-admin.token
```

`verify` 只讀取 WebUI 與資料庫。它驗證管理員身分、檔案 owner、檔名、來源、生成類型及宣告雜湊，
再下載內容計算 SHA-256；新收條以原始輸出大小為上限，沒有大小資訊的舊收條沿用 20 MB。
`reconcile` 另將核對結果寫入加密稽核紀錄，
只有完整通過核對才原子標記為 `stored`。核對與更新之間收條若已改變，會拒絕套用舊結果。
找不到檔案、內容不符或服務無法連線，都保留原狀態，不開放重送。

人工追蹤可使用：

```bash
python -m examples.official_document_workflow.cli.output_receipts annotate \
  --owner USER_ID --output-id OUTPUT_ID --actor OPERATOR_ID \
  --outcome investigating --note '已建立內部追蹤單，等待儲存管理員核對'
```

人工結果可為 `needs_review`、`reported_missing` 或 `investigating`。actor 是資料庫操作員
提供的識別；自動核對的 actor 則取自 WebUI 驗證過的管理員身分。註記不會變更輸出狀態。
以上所有命令都不會重新建立 CSV，也不會重跑文件處理。`unknown` 持續需要核對；
只有原流程確認未寫入的 `not_written` 才具備原有重試語意。

`inspect` 現在也提供輸出 `kind`、背景工作 `job` 與原始 `size`。同一背景工作的輸出 ID
依 owner、工作、來源及輸出角色固定；同名 EIP 附件另以來源識別區分。檔案完成後即保存
收條，因此工作清單的成果投影更新失敗時，重新查詢仍能讀回已確認的下載成果。
狀態 `deleted` 表示服務已明確移除該輸出，不自動重建，也不允許以舊核對結果復原狀態。

`deleting` 表示刪除意圖已持久化；`delete_unknown` 表示尚未證明下載紀錄與儲存物件都已
移除。這兩種狀態不視為刪除成功，也不會被背景程序重新 DELETE。管理員可使用
`list --owner USER_ID --state delete_unknown` 與 `inspect` 查詢；人工註記仍不變更狀態。
`inspect` 的 `revision` 用於辨識過期快照，`path` 是加密保存的刪除核對目標，輸出應私密保存。

背景修復每 5 秒執行一次，每批最多 32 筆、同時最多核對 4 筆。新寫入與刪除先保留
30 秒寬限；每筆核對以 60 秒 lease 取得，失敗採 2 秒起、最多 300 秒退避。
只能在內容核對通過後標記 `stored`，或確認下載紀錄及儲存物件都不存在後標記 `deleted`。
找不到寫入成果、內容不符或儲存服務無法確認時，繼續保留結果不明，不重送任何檔案操作。

收條保存 `projection_pending` 通知狀態。檔案已完成但工作成果投影提交失敗時，背景核對
可補齊收條，再通知工作／聊天清單讀回成果。通知失敗也會持久退避；不重跑文件或模型。
沒有對應發布器的獨立程序不會清除此旗標，避免吞掉應由宿主處理的通知。
發布器也必須確認工作屬於目前應用；其他應用找不到的工作會繼續保留待送，不當作成功。


背景核對每輪最多 32 筆，同時最多 4 筆；只有空閒名額才領取一筆 60 秒 lease，
每 15 秒續租。核對及成果發布各有 45 秒期限；已開始的同步儲存讀取會先等待完成，
期間繼續續租，避免取消後遺留執行緒並反覆建立替代讀取。

核對結果分成 `verified`、`not_found`、`mismatch`、`unavailable`。
暫時不可用及尚未找到的輸出維持退避；連續 3 次內容或來源不符時設定 `review_required`，
停止自動內容核對，仍保留尚未送達的成果通知。成功核對或其他非 mismatch 結果會中斷
連續 mismatch 計數。`list` 提供最近結果、原因及計數；`inspect` 的 `verification`
提供最近核對證據。內容雜湊／大小證據加密保存，錯誤原因只保存固定代碼，不保存例外訊息。
管理員可先 annotate 追蹤；修正來源問題後使用 reconcile，通過相同檔名、來源、大小及
實際 SHA-256 核對才清除人工核對旗標並完成收條，不能僅以註記重新開放輸出寫入。

刪除核對另保存 `deleted`、`residual`、`unavailable`、`mismatch` 四種結果。
`residual` 表示下載紀錄或儲存物件仍有殘留；`unavailable` 表示無法確認儲存狀態，
不再混用「找不到」原因。證據包含兩端是否存在，以及物件仍在時的實際雜湊／大小。
路徑或內容不符會進入 mismatch 計數及人工核對，背景程序仍不執行刪除。
一般檔案核對成功也保留實際 SHA-256 與大小；交付使用同一次核對的檔案物件，
不為補寫證據再次讀取內容。此變更沿用公文 migration v9。
