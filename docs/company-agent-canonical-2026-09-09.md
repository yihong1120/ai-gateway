# Company Agent：移除舊相容層

日期：2026-09-09。依使用者確認，以 Open WebUI 為唯一正式使用入口，
不再維護舊 Python import 路徑。

## 已完成

- 刪除 33 個根目錄相容模組，包含 agent.py、contracts.py、web_pipe.py、
  web_service.py、web_router.py、tools.py、storage.py、checkpoints.py 等。
- 刪除專門載入舊套件的 tests/rolling_compatibility.py；不再提供舊版程式接續驗證框架。
- WebUI Function 直接引用 company_agent.api.pipe.run_pipe 與 runtime.errors.AgentError。
- 已同步更新 WebUI 資料庫中的 company_work_assistant Function 內容，不只修改磁碟範本。
  保留原名稱、擁有者、啟用狀態、全域狀態及 metadata；沒有重設模型或權限。
- EIP／WebUI 啟動器直接安裝 company_agent.api.router。
- 內部程式、維運腳本及測試直接引用實作模組；Context 統一從 runtime.context 取得。
- 移除只為相容別名而存在的測試斷言，改驗證實際實作位置及舊模組不再存在。

保留工作恢復、checkpoint 狀態、使用者與來源隔離、預算、去重及授權檢查。
移除的是 Python 路徑相容層，不是刪除工作資料或取消保護機制。
本次沒有修改 EIP 行程、附件、NAS 綁定或重新執行公文流程。

## 測試

| 項目 | 結果 |
| --- | --- |
| 完整回歸 | 315 項通過，0 略過，125.055 秒；311 項公司助理測試＋4 項 WebUI Function／啟動器測試 |
| 真實整合 | PostgreSQL、CAD／Office 容器及本機 OCR 開啟 |
| wheel／sdist | 建置成功；套件不再包含 33 個舊模組 |
| wheel 直接載入 | 18 項架構／UI 測試通過 |
| 安裝後文件解析 | 舊入口無法找到；新路徑可實際產生 Word 並以子程序重新解析 |
| JavaScript | 工作台與統一連線頁兩套測試通過 |
| 部署後 | health 200、未登入工作台 303、6 個資源內容与新套件一致、工作清單與 EIP 狀態 200 |
| 管理員／一般使用者 | 兩種角色的頁面、API、公司模型選單與計費身分驗證均通過 |
| 錯誤操作 | 無效預覽／確認／額度延長請求回傳 400；缺少 CSRF 回傳 403 |

後端模型／NAS／EIP／Tavily 外部操作使用測試替身。
正式 WebUI 驗收未送出生成工作、未呼叫付費模型、未寫入 EIP；
計費驗證只確認模型身分綁定，不宣稱已重新核對實際帳單扣款。

完整回歸過程也修正兩個既有的不穩定測試：

1. ToolNode 的獨立讀取可並行完成，不應依執行緒先後斷言。
   改確認兩筆指定讀取各執行一次；仍檢查整批參數修正前完全未執行。
2. 32 字元十六進位來源 handle 可能隨機包含「123」，不代表洩露原始文件 ID。
   改核對精確回傳欄位、handle 格式及伺服器端對原始 ID 的解析。
   跨工作拒絕、綁定版本重驗證及私人 URL 不外洩的原有斷言均保留。

不是單純重跑至綠燈，也沒有跳過失敗案例或放寬執行權限。

## 部署與備份

已確認公司工作、EIP 自動處理及 EIP 手動工作均為 0 後，
重新啟動 openwebui.service。服務目前 active，已完成上述線上檢查。
本次只調整 Python 入口，沿用上一輪已驗證的 CAD／Office 容器，沒有重建或更換模型。

刪除前快照與 Function 原設定保存於：

```text
/tmp/company-agent-canonical-Okd5Zd/
├── company_agent/            # 移除前程式，包括被刪除的模組
├── company_agent_pipe.py
├── serve.py
├── stored-function.json      # 私人權限保存
├── pyproject.toml
├── verified-tests.log
├── deployment-check.json
└── webui-smoke.log
```

刪除檔案可從快照恢復；此路徑為暫存備份，不是永久備份策略。
不應為程式回退而刪除或重設正式 PostgreSQL 工作、checkpoint 或成果。

現行維護位置見 [公司助理 README](../company_agent/README.md)。
上一輪保留相容層的說明是歷史紀錄，不再代表目前設計。
