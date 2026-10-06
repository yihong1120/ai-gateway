# Company Agent 例外處理精簡與驗證

2026-09-10。已修改程式、完成回歸與套件驗證，並更新 Open WebUI 服務。

## 修改範圍

本次修改 10 個既有 Python 模組，新增 runtime/receipts.py，另新增測試與更新架構指南。
統計涵蓋這 11 個非測試模組（新 helper 也計入）：

| 項目 | 修改前 | 修改後 |
| --- | ---: | ---: |
| try 區塊 | 39 | 28 |
| except 分支 | 36 | 26 |
| 捕捉 Exception／BaseException 的分支 | 3 | 1 |

這是上述模組的局部統計，不代表整個 company_agent 已刪除所有 try/except。
保留的廣泛捕捉為 NAS 登入初始化失敗時關閉連線並重新拋出原錯誤。

## 實作

- runtime/history.py、runtime/read_repair.py、services/office/revision.py 的八處
  JSON 物件解析統一呼叫 runtime/receipts.py。非法 JSON、非物件、錯誤編碼不作為
  有效委派、審核或讀檔證據；原先部分非物件 JSON 會造成 AttributeError，現已處理。
- 檔案 parser、Office／CAD 容器、NAS／Tavily 邊界改抓實際的傳輸、JSON 或編碼錯誤，
  不再先捕捉自己的 AgentError 再原樣拋出；公開錯誤仍不附帶原始回應或憑證。
- spreadsheet_books 改用標準 contextmanager／ExitStack／closing。讀取、分析、修改
  使用 with，移除重複 try/finally 關閉 Workbook。開第二份工作簿失敗仍關閉第一份；
  其中一份 close 失敗，仍嘗試關閉其他已開啟資源。
- Excel 修改移除重複輸入封裝驗證；spreadsheet_books 仍驗證输入，修改後仍驗證輸出。
- CAD 逾時轉譯放在 run_bounded，外層不再重複呼叫 docker rm。
- NAS 金鑰取得放在解密捕捉外；僅針對 InvalidTag／ValueError 處理解密及內容格式失敗，
  不再把任意程式錯誤都包成「憑證無法解密」。

未更改 AgentError 的繼承關係、工具 schema、角色授權、GRAPH_VERSION、State 或確認流程。
沒有移除 ACL、本人計費、來源雜湊、去重、資源上限、工作終止狀態紀錄或必要清理。
沒有新增自動重試，也沒有把失敗預設為成功。

## 測試

- 全套 Python **348 項通過、無跳過**，132.317 秒。
- 本次新增 test_error_boundaries.py 的 14 項測試，驗證格式錯誤、證據拒絕、錯誤遮罩、
  正常／異常資源關閉、NASAuthError 類型保留、CAD 逾時僅清理指定容器一次。
- 初步針對性測試 24 項、最終架構與例外測試 38 項通過。
- wheel／sdist 建置成功；直接從 wheel 載入的 **46 項測試通過**。
- wheel 獨立安裝後，以真實 parser 子程序修改 Excel A1 的 10 為 12，再重新讀取驗證。
- 兩套 Node UI 測試通過（工作台、統一帳號連結頁）。

完整測試使用真實 PostgreSQL 的隔離測試 schema、CAD／Office 容器及本機 OCR。
NAS、EIP、Tavily 與模型业务呼叫使用替身，不代表對正式遠端系統重新做寫入驗收。

## 上線

重啟前再次確認公司助理排隊／執行、EIP 自動處理中、EIP 手動工作均為 0。
已重啟 openwebui.service，服務為 active。

管理員／一般使用者均通過頁面、工作列表、模型可見性、計費身分檢查；
無效確認參數回應 400、CSRF 403。健康檢查 200、匿名頁轉登入 303，
六個 UI 資源與套件一致，EIP 狀態 API 200。
上線 smoke 的付費模型請求為 0，未提交新工作或修改真實行程。

程式備份及測試／部署紀錄：`/tmp/company-agent-errors-lue5sM/`。
本次未變更 Docker image、未刪除正式工作紀錄或使用者檔案。
回復時應先停止背景工作，再逐項比對備份還原此次修改，避免覆蓋後續變更。
