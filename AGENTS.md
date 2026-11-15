# 跨 repo 認證維護

修改帳密登入、密碼恢復、信箱驗證、refresh／session 或相關 HTTP 契約時，必須同步檢查
相鄰的 `Construction-Hazard-Detection` 與 `visionnaire-flutter-dev`，並完成適用的程式、
客戶端、文件及回歸測試更新，不能只修改本 repo。

`Construction-Hazard-Detection` 已採 Keycloak／OIDC，不可直接搬入 gateway 的本機
密碼登入、token 表或 migration。共用的是適用的安全行為與 HTTP 契約，各登入模式應
維持自己的身份權威。修改其他 repo 時保留其既有未提交變更。

契約及驗證範圍見 `docs/authentication-cross-repo.md`。
