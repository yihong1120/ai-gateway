# company_agent 冗餘清理（2026-09-10）

## 實際刪減

- 移除八個 `agents/<role>/schemas.py`：只有轉匯出，改由 adapter／測試直接引用 `services/<domain>/schemas.py`。
- 移除 coordinator、knowledge、document、data、engineering 五個只有 `select_tools` 別名的 `tools.py`。EIP、research、reviewer 的實際 adapter 保留；角色入口仍以 SPEC、agent.py、prompt.md 分類。
- 共用建構器直接使用 `spec.select_tools`，不再經過八份別名或多傳一個函式參數。
- 清除 115 個未使用 import（包含被刪檔案內的 30 個）、兩個無用區域變數。11 個不使用的 tuple 回傳項改成明確忽略，不刪掉資源管理或必要呼叫。
- EIP `prepare`／`process` 移除未使用的 `call_id` 參數；仍以既有內容指紋去重，不改變工具公開 schema 或重新匯入政策。
- EIP 文件來源登記共用既有 `remember`，刪除重複的 UUID／SQL 登記程式。

非測試 Python 原始碼：10,174 → 10,013 行，淨減 161 行；Python 檔案減少 13 個。沒有以壓縮既有排版來增加刪減數字。

## 未刪除的必要機制

FastAPI 的登入依賴參數、Open WebUI 的 Pipe 介面與框架回呼可能未在函式本文引用，但仍有用途。保留帳號／工作隔離、本人計費、工具白名單、寫入確認、版本／雜湊查核、去重、checkpoint 及失敗狀態；不把動態註冊的工具、Pydantic 驗證器或 HTTP 路由誤當成死碼。

## 驗證方式

- Ruff 0.16.6：`uvx ruff check company_agent --select F,RUF059`，檢查未使用 import／變數、未定義名稱及重複定義；不代表已啟用全部 Ruff 規則。
- Vulture 的 90% 以上高可信度死碼掃描；低可信度結果逐項確認框架用途，不直接刪除。
- 371 項回歸測試，包含 PostgreSQL 隔離 schema、CAD 容器、Office、OCR、聊天壓縮與 Open WebUI 入口。
- wheel 內另跑 46 項架構／工具契約／角色／UI／錯誤邊界測試；兩組 Node 前端測試。
- 不呼叫付費模型，也不寫入真實 EIP／NAS 資料。

修改前完整程式備份及測試記錄位於 `/tmp/company-agent-cleanup-vtotBV/`；被刪的 13 個程式檔可從該備份還原，沒有刪除使用者聊天、附件或工作資料。

## 驗收結果

最終版本完整 371 項測試通過（145.503 秒、無略過），wheel 46 項與兩組 Node 測試通過；Ruff 指定規則及 Vulture 高可信度掃描無問題。

確認公司助理、EIP 自動／手動執行中工作均為 0 後，已重啟 Open WebUI。部署後管理員與一般使用者入口、模型可見性、本人計費身分、六個套件資源、工作清單及 EIP 狀態正常；健康檢查 200、匿名導向登入 303、無效參數 400、CSRF 防護 403。本次模型及外部服務使用測試替身，付費模型請求為 0，不宣稱已重新實測所有外部業務操作。
