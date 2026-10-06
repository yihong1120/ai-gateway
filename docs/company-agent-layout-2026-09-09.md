# Company Agent 第二階段重構驗收

> 歷史紀錄：後續依使用者要求已移除本文保留的相容層；現況見 [單一入口驗收](company-agent-canonical-2026-09-09.md)。

日期：2026-09-09（台北時間）。

## 完成範圍

依使用者提供的結構完成 agents／services／runtime／api／ui 分層，
不是只建立目錄或把原本大檔移到另一個大檔。主圖 graph.py 為 86 行，
僅負責官方 StateGraph／ToolNode 組裝。

- 八個角色均有 agent.py、prompt.md、tools.py、schemas.py，並註冊實際建構函式。
- 角色共用一份模型綁定與工具完整性檢查；移除重複綁定。
- 讀檔、NAS、Office、CAD、研究、EIP 的業務實作已移至 services。
- reviewer 的產出解析、document 的修訂操作移至業務層；Agent 保留薄工具 adapter。
- 模型、本人計費、輔助分析、身分驗證、PostgreSQL、checkpoint 與預算集中於 runtime。
- WebUI pipe、路由及工作佇列移至 api；HTML／JS／CSS 移至 ui。
- UI 改以套件資源讀取；Dockerfile、Docker 建置白名單及 parser 子程序路徑同步調整。
- 根目錄保留舊 Python import 的薄相容入口，不保留第二份業務實作。
- 根目錄舊版 5 個 UI 副本已移除，ui 為唯一維護位置；舊快照可恢復。
- 沒有更換模型、變更依賴版本、放寬權限、修改正式行程或重新處理公文。

詳細目錄與日常修改位置見 [公司助理 README](../company_agent/README.md)。

## 行為與資料相容

保留 GRAPH_VERSION=20、節點、邊、State 欄位、reducer、完成協定、
使用者／工作隔離、來源 SHA256、確認與操作去重。
仍為單一官方 LangGraph 圖中的循序多角色協作，並非新增並行子圖。

與重構前完整套件快照比對：

- Settings JSON schema 一致。
- 16 個主要檔案工具的 schema 及 description 一致。
- 8 個角色的提示詞與工具範圍一致。
- 架構 fixture 驗證原節點、邊、State 與角色權限不變。

不透過重寫 fixture 或放寬斷言使測試通過。

## 測試結果

| 測試 | 結果與邊界 |
| --- | --- |
| 公司助理完整回歸 | 310 項通過，0 略過；真實 PostgreSQL、CAD／Office 容器及本機 OCR |
| 完整舊套件 → 新套件 | 本人同意、本人拒絕均通過；真實 checkpoint 接續 |
| 新套件 → 舊套件回退 | 通過；既有私人 Word 保留、不重複產生、額度不重設、他人無法接續 |
| 新路徑重建容器 | CAD／Office 相關 32 項通過；測試使用新建映像，不僅是舊快取映像 |
| 安裝套件驗證 | wheel／sdist 建置成功；從 wheel 獨立載入的 17 項架構／UI 測試通過 |
| 安裝後 parser | 臨時目錄安裝 wheel；實際 Word 產生與解析子程序通過 |
| JavaScript 畫面 | 工作台、統一帳號連線兩套測試通過：安全 DOM、CSRF、登入失效、部分服務失敗 |
| 正式服務讀取驗收 | health 200、未登入 303、6 個已登入資源與新套件位元一致、工作清單及 EIP status 200 |

以上測試有重疊，不將 32／17 等子集再加總成不同案例數。
模型、NAS、EIP 寫入、Tavily 使用測試替身；沒有將這次回歸宣稱為付費模型或正式遠端寫入驗收。
正式服務驗收僅讀取 localhost 頁面與狀態，沒有送出新工作或修改 EIP 行事曆。

過程曾發現搬移後的 research／EIP 工具引用及 scenario 的驗證身分測試替身位置不一致。
已改為引用實際擁有功能的 canonical 模組，重跑完整套件通過，而非停用失敗案例。
LangGraph 套件目前仍輸出既有的 serializer 未來預設值變更警告；不是測試失敗，
本次不藉重構順便升級依賴或改變 checkpoint 反序列化策略。

## 部署與回退

部署前確認公司助理、EIP 自動處理與手動工作的活動數均為 0；
保留舊映像，將已測試的新映像套用既有 :1 標籤，再重啟 openwebui.service。
未強制中斷工作，未修改正式業務資料表或 EIP 事件。

本機回退資料：

- 原始碼快照：/tmp/company-agent-layout-HYKzRb/company_agent/
- 原 pyproject.toml：/tmp/company-agent-layout-HYKzRb/pyproject.toml
- 舊 CAD 映像：ai-gateway/company-cad:before-layout-20260909
- 舊 Office 映像：ai-gateway/company-office:before-layout-20260909
- 新建測試映像：ai-gateway/company-cad:layout-20260909、ai-gateway/company-office:layout-20260909
- 完整測試與部署紀錄：/tmp/company-agent-layout-HYKzRb/

快照位於暫存目錄，不是永久備份。需回退時先確認沒有活動工作，再恢復對應程式與映像、
重新啟動及驗證；不要還原／刪除正式 PostgreSQL 工作或產出來處理程式回退。
第一階段原始單檔重構紀錄另見 [先前報告](company-agent-refactor-2026-09-09.md)。
