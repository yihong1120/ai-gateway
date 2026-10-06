# Company Agent 根目錄精簡驗收

日期：2026-09-09。這次是檔案分類、引用更新及維護入口整理，不是改寫 LangGraph 執行引擎。
現行維護導覽見 [公司助理 README](../company_agent/README.md)。

## 實際變更

根目錄從 40 個檔案減為 7 個：.gitignore、README.md、__init__.py、graph.py、state.py、config.py、schemas.py。

| 分類 | 內容 |
| --- | --- |
| runtime/capabilities.py | 原本根目錄的能力清單；所有呼叫方直接引用新模組 |
| ops/ | deploy_webui、deploy_connections、smoke_webui |
| evals/ | benchmark、scenario_suite、smoke、smoke_research、smoke_vision；新增成本與使用說明 |
| configs/ | 兩份設定範本，未套用或覆蓋正式設定 |
| docs/guides/ | 8 份架構與操作指南 |
| docs/archive/ | 12 份歷史報告，均標註為歷史紀錄 |

共搬移 31 個檔案，不保留舊路徑 alias 或轉接模組。
README 改為現行入口、目錄、能力界線、計費與測試導覽；同步修正文件相對連結與管理命令。

已移除未使用的 __main__.py 獨立 CLI、prepare_postgres.py 一次性 SQLite 切換工具。
另將只剩舊 .pyc 的 cad_worker/ 目錄移至備份；實際 CAD worker 位於 services/cad/worker/。
正式資料庫、工作紀錄、NAS／EIP 帳號綁定、附件、checkpoint 金鑰及 test_outputs/ 均未刪除。

移動部署程式後，模板位置已改為正確的儲存庫路徑，並在任何部署寫入前讀取模板。
缺少來源模板時會直接失敗，不先改 WebUI 設定。部署工具仍需在來源工作區明確執行，
不會隨套件 import 或服務啟動而自動部署。

## 驗證結果

| 驗證 | 結果 |
| --- | --- |
| company_agent 全套及 WebUI 正式入口測試 | 320 項通過，0 失敗、0 略過，125.725 秒 |
| 真實整合 | PostgreSQL 隔離 schema、CAD 容器、Office 容器、本機 OCR 啟用並通過 |
| 前端 | 工作台及帳號連線頁兩組 Node 測試通過 |
| wheel／sdist | 建置成功；搬移的程式、提示詞、UI、設定範例及文件均收錄；不夾帶 test_outputs 或已移除程式 |
| wheel 直接載入 | 21 項架構、目錄與 UI 資源測試通過 |
| wheel 獨立安裝 | 9 個新路徑模組載入成功；以實際子程序產生 DOCX 並讀回文字成功 |
| 文件連結 | 公司助理 README、指南及歷史報告的本地相對連結檢查通過 |
| 舊引用掃描 | 現行程式與命令未殘留本次搬移前的模組引用 |

新增的回歸檢查涵蓋根目錄精簡契約、範本 schema 與套件資源、正式執行層不依賴 ops／evals／tests、
部署模板正確位置，以及模板缺失時不得開始部署。

遠端模型、NAS、EIP 及 Tavily 業務呼叫使用測試替身；以上不表示本次重跑所有遠端寫入流程。
沒有付費模型請求，沒有新增、修改或刪除 EIP 行程。

## 上線檢查

確認公司助理工作、EIP 自動處理及手動工作均無 queued／running 任務後，重新啟動 openwebui.service。
服務 active；/health 為 200，未登入存取 /agents 為 303。

管理員與一般使用者各自檢查通過：工作台、成果清單、NAS 綁定狀態、帳號連線頁、EIP 狀態及模型選單。
本人模型計費身分核對通過；無效 preview／confirm／extend 請求為 400，缺少 CSRF 防護為 403。
六個登入後 UI 資源與目前套件內容一致，維持 no-store 與 nosniff。
資料庫內 Function 仍引用 canonical api.pipe，無須重新安裝或重設使用者模型設定。

## 備份及證據

本次整理前的 company_agent/ 與 pyproject.toml 備份在 /tmp/company-agent-tidy-2wHDYx/。
移除的兩個原始碼檔可從其 company_agent/ 子目錄恢復；舊 CAD 快取在 retired-cad-cache/。
tests.log、build.log、restart.log、webui-smoke.log、deployment-check.log 及 dist/ 保存本次驗證證據。
這是暫存備份，不取代正式備份制度；/tmp 被系統清理後不保證保留。
