# 共用執行基礎

此套件整合原本根目錄的執行基礎；呼叫端直接引用 `examples.shared.runtime`，沒有舊命名空間的轉接副本。

| 模組 | 責任 |
|---|---|
| `database` | PostgreSQL pool scope、交易連線及共享資源釋放 |
| `http_clients` | 應用持有的具名 HTTP clients 與設定一致性 |
| `io` | 阻塞操作轉接；取消時等待既有交易完成 |
| `workers` | worker 健康狀態、退避與恢復 |
| `jobs` | 應用工作服務綁定 |
| `concurrency`、`usage` | 文件／OCR／模型名額及可信用量歸屬 |
| `chat`、`progress` | 目前聊天分支、工作觀察與對外進度 |
| `module_registry` | 動態模組引用與有界淘汰 |
| `modules` | 在檔案上方宣告選用 host／worker 及循環依賴，保留必要的載入時機 |
| `installation` | 應用擴充的冪等安裝與失敗回復 |
| `retention` | 容量盤點與明確啟用的快取清理 CLI |

共用套件本身不組裝產品應用。WebUI factory、啟動器與使用者查詢 adapter 在
`examples.openwebui`；LiteLLM 設定仍在其專屬部署目錄，不以代理設定取代本機交易、
OCR 排程或背景工作恢復。

容量盤點入口：`python -m examples.shared.runtime.retention --help`。
清理需要明確指定條件與套用選項，不會在匯入此套件時執行。

Python 的一般依賴統一放在檔案上方；型別依賴可使用同區塊的 `TYPE_CHECKING`。
只有需要先載入設定、可選的宿主／worker、循環依賴或隔離測試 fixture，才使用
`ModuleReference` 在檔案上方宣告固定模組名稱，在原本的操作邊界解析。
它沿用 Python 的匯入鎖與模組快取，不保存使用者、憑證或額外的模組副本。
WebUI 的 `app.py` 先載入設定，再交由 `_host.py` 組裝宿主；獨立 MCP 不載入 WebUI。
`tests/test_import_layout.py` 檢查整個 repo，防止函式內或執行敘述之後再次出現 import。
