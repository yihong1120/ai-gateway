# 共用設定與宿主整合

`config.py` 提供 `read_service`、`read_services` 與服務設定投影操作，
從同一份中央 TOML 取得指定區塊，維持程序環境變數優先。
它只讀取及驗證設定；程序啟動、Docker Compose 與舊設定轉存由
`examples.deployment.cli` 管理，應用不必匯入部署工具。

`host.py` 提供管理服務使用的 `HostIntegrations` 邊界，整合通知快取、媒體路徑與串流 overlay。
呼叫端使用 `examples.shared.host.integrations`，宿主可在應用組裝時提供對應操作。

這些整合採延遲載入；缺少宿主功能時，相關路由會在執行副作用前回覆 503。
帳號、用量與部署註冊的核心功能不需要載入通知或串流宿主套件。

跨產品的資源與工作執行介面集中在 [runtime/](runtime/README.md)：
PostgreSQL／HTTP 連線池、I/O 取消處理、工作服務綁定、背景監督、處理名額與用量歸屬。
這些是應用執行程式，與 `examples/litellm` 的代理設定與部署工具各司其職。
WebUI 組裝位於 `examples/openwebui`；已驗證使用者查詢的宿主 adapter 位於
`examples.identity.adapters.webui`，只在操作時延遲載入宿主。
