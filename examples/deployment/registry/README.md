# 部署註冊服務

由 `examples.db_management.app:app` 掛載的 router，前綴為 `/deployment-registry`。

- `router.py`：註冊碼交換與部署文件查詢入口。
- `schemas.py`：HTTP 請求與回應契約。
- `service.py`：部署／租戶狀態、流程協調與註冊文件簽署。
- `enrollments.py`：註冊碼驗證、限流與原子兌換。

Redis 原子計數使用共用實作；沒有獨立的簽署轉接或私有 Lua 副本。

使用共用認證模型、資料庫 session 與 Redis 依賴；不另建應用或第二份 ORM registry。
設定與啟動方式見 [服務總覽](../../README.md)。
